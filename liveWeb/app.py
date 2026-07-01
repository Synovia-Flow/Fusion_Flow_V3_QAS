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

from flask import Flask, Response, abort, jsonify, send_from_directory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "tools"))
import export_blueprint as xb  # reuse load_conn / conn_str / build  # noqa: E402

app = Flask(__name__, static_folder=None)
CACHE_TTL = 30  # seconds
_cache = {"ts": 0.0, "data": None}


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
