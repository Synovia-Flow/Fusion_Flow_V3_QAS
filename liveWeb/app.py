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

# Portal actions run real jobs / TSS calls, so they are OFF unless explicitly enabled.
ACTIONS_ON = os.environ.get("PORTAL_ACTIONS_ENABLED", "").lower() in ("1", "true", "yes", "on")
# verb -> (runner module, the CFG param that scopes it to one movement)
VERB = {
    "promote":   ("promote_ens", "SUBMISSION_MOVEMENT_KEY"),
    "submit":    ("submit_ens", "SUBMISSION_MOVEMENT_KEY"),
    "mirror":    ("mirror_ens", "SUBMISSION_MOVEMENT_KEY"),
    "update":    ("update_ens", "SUBMISSION_MOVEMENT_KEY"),
    "cancel":    ("cancel_ens", "SUBMISSION_MOVEMENT_KEY"),
    "reprocess": ("reprocess_engine", "PROCESSING_MOVEMENT_KEY"),
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
    """Run a real job scoped to one movement (dry-run governed by SUBMISSION_DRY_RUN).
    Disabled by default — set PORTAL_ACTIONS_ENABLED=1 to allow. Everything the runner
    does is tracked in EXC / API.Call / LOG, per the platform design."""
    if not ACTIONS_ON:
        return jsonify({"ok": False, "disabled": True,
                        "error": "Portal actions are disabled. Set PORTAL_ACTIONS_ENABLED=1 on the service."}), 403
    if verb not in VERB:
        abort(404)
    mk = (request.args.get("mk") or (request.get_json(silent=True) or {}).get("mk") or "").strip()
    if not mk:
        return jsonify({"ok": False, "error": "mk (MovementKey) required"}), 400
    module_name, mk_param = VERB[verb]
    conn = _connect(); cur = conn.cursor()

    def getp(k):
        r = cur.execute("SELECT ParameterValue FROM CFG.Application_Parameters WHERE ParameterKey=?", k).fetchone()
        return r[0] if r else None

    def setp(k, v):
        cur.execute("UPDATE CFG.Application_Parameters SET ParameterValue=? WHERE ParameterKey=?", v, k)

    old_mk, old_max = getp(mk_param), getp("SUBMISSION_MAX_ROWS")
    try:
        setp(mk_param, mk)
        setp("SUBMISSION_MAX_ROWS", "1")
        mod = importlib.import_module(module_name)
        code = mod.run()
        row = cur.execute("SELECT Fusion_Status, Tss_Status, declaration_number "
                          "FROM STG.BKD_ENS_Header WHERE MovementKey=?", mk).fetchone()
        status = {"fusion": row[0], "tss": row[1], "decl": row[2]} if row else None
        return jsonify({"ok": code == 0, "verb": verb, "job": module_name, "mk": mk, "status": status})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "verb": verb, "mk": mk, "error": str(e)}), 500
    finally:
        if old_mk is not None:
            setp(mk_param, old_mk)
        setp("SUBMISSION_MAX_ROWS", old_max if old_max is not None else "0")
        conn.close()


@app.route("/api/edit", methods=["POST"])
def api_edit():
    """Edit STG payload fields for a movement (whitelisted). The next Update pushes
    them to TSS. Disabled unless PORTAL_ACTIONS_ENABLED=1."""
    if not ACTIONS_ON:
        return jsonify({"ok": False, "disabled": True, "error": "Portal actions are disabled."}), 403
    body = request.get_json(silent=True) or {}
    mk = (body.get("mk") or "").strip()
    fields = {k: v for k, v in (body.get("fields") or {}).items() if k in EDITABLE}
    if not mk or not fields:
        return jsonify({"ok": False, "error": "mk and at least one editable field required"}), 400
    conn = _connect(); cur = conn.cursor()
    try:
        sets = ", ".join(f"[{k}]=?" for k in fields) + ", UpdatedAt=SYSUTCDATETIME()"
        cur.execute(f"UPDATE STG.BKD_ENS_Header SET {sets} WHERE MovementKey=?", *fields.values(), mk)
        return jsonify({"ok": True, "mk": mk, "updated": list(fields)})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
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
