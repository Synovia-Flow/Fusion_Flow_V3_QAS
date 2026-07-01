#!/usr/bin/env python3
"""Fusion Flow V3 QAS - dump TSS responses for submitted ENS headers to disk.

For every submitted movement (STG.BKD_ENS_Header with a declaration_number), GETs
the header back from TSS and writes ONE JSON file per record - containing the full
REQUEST and the full RESPONSE - to an output folder, for offline analysis (to design
the TSS.* live-mirror table and the next process step). Each call is also logged to
API.Call.

This is a READ (GET) - safe - so it contacts TSS regardless of SUBMISSION_DRY_RUN,
using SUBMISSION_ENV (default TST). Output folder: SUBMISSION_JSON_DIR, else
<repo>/Development/json. An optional positional arg overrides the folder.

    python fetch_submitted_json.py ["D:\\some\\folder"]

Controls (CFG.Application_Parameters): SUBMISSION_CLIENT, SUBMISSION_ENV,
SUBMISSION_MOVEMENT_KEY (single), SUBMISSION_MAX_ROWS (cap; 0 = all),
SUBMISSION_API_BASE_PATH.
"""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from pathlib import Path

try:
    from .submission_db import SubmissionDb, load_db_config, DEFAULT_INI, REPO_ROOT, now_utc, ENS_READBACK_FIELDS
    from .tss_client import TssClient
except Exception:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from submission_db import SubmissionDb, load_db_config, DEFAULT_INI, REPO_ROOT, now_utc, ENS_READBACK_FIELDS  # type: ignore
    from tss_client import TssClient  # type: ignore

ENDPOINT = "headers"
FIELDS = ",".join(ENS_READBACK_FIELDS)   # header GET requires a fields= list (min one)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()) or "unknown"


def run(ini_path: Path = DEFAULT_INI, output_override: str | None = None) -> int:
    db = SubmissionDb.connect(load_db_config(ini_path))
    client = (db.param("SUBMISSION_CLIENT", "BKD") or "BKD").strip().upper()
    env = (db.param("SUBMISSION_ENV", "TST") or "TST").strip().upper()
    base_path = (db.param("SUBMISSION_API_BASE_PATH", "/x_fhmrc_tss_api/v1/tss_api") or "").strip()
    target_mk = (db.param("SUBMISSION_MOVEMENT_KEY", "") or "").strip()
    try:
        max_rows = int((db.param("SUBMISSION_MAX_ROWS", "0") or "0").strip())
    except ValueError:
        max_rows = 0

    out_dir = (output_override or db.param("SUBMISSION_JSON_DIR", "")
               or str(REPO_ROOT / "Development" / "json")).strip()

    found = done = failed = 0
    try:
        db.open_execution("MONITORING", client, env, "read")
        # READ-only GET: contact TSS for real regardless of dry-run.
        api = TssClient.from_cfg(db, env, client, base_path, dry_run=False)
        os.makedirs(out_dir, exist_ok=True)
        db.log("START", f"Dump TSS responses for submitted {client} ENS -> {out_dir} (env={env})"
               + (f" MK={target_mk}" if target_mk else ""))

        top = f"TOP ({max_rows}) " if max_rows > 0 else ""
        sql = (f"SELECT {top}StgID, MovementKey, declaration_number, Fusion_Status "
               f"FROM STG.BKD_ENS_Header WHERE ClientCode = ? AND declaration_number IS NOT NULL")
        params = [client]
        if target_mk:
            sql += " AND MovementKey = ?"; params.append(target_mk)
        sql += " ORDER BY StgID"
        rows = db.q(sql, *params)
        found = len(rows)
        db.log("SOURCE", f"{found} submitted movement(s) with a declaration_number.")

        for r in rows:
            mk = (r.get("MovementKey") or "").strip()
            decl = (r.get("declaration_number") or "").strip()
            try:
                result = api.call("GET", f"{ENDPOINT}?reference={decl}&fields={FIELDS}", None)
                db.log_call(process="MONITORING", resource="Declaration Header", op_type="read",
                            movement_key=mk, declaration_number=decl, result=result)

                body = TssClient.parse_json(result)
                record = {
                    "client_code": client,
                    "movement_key": mk,
                    "declaration_number": decl,
                    "stg_status": r.get("Fusion_Status"),
                    "env": env,
                    "fetched_at_utc": now_utc().isoformat(sep=" "),
                    "request": {
                        "method": result.get("method"),
                        "url": result.get("request_url"),
                        "headers": result.get("request_headers"),   # Authorization redacted
                    },
                    "response": {
                        "status_code": result.get("status_code"),
                        "ok": result.get("ok"),
                        "duration_ms": result.get("duration_ms"),
                        "headers": result.get("response_headers"),
                        "body": body if body is not None else result.get("response_text"),
                        "error": result.get("error"),
                    },
                }
                fname = f"{_safe(decl)}__{_safe(mk)}.json"
                Path(out_dir, fname).write_text(
                    json.dumps(record, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
                db.log("WROTE", f"MK={mk} decl={decl} status={result.get('status_code')} -> {fname}",
                       "OK" if result.get("ok") else "WARN")
                done += 1
                if not result.get("ok"):
                    failed += 1
                db.commit()
            except Exception as e:  # noqa: BLE001
                failed += 1
                db.conn.rollback()
                db.log_error("FETCH_ROW", f"MK={mk}: {e}", type(e).__name__, traceback.format_exc())

        db.finish("COMPLETED" if failed == 0 else "COMPLETED_WITH_WARNINGS", found, done, failed)
        db.log("FINISH", f"found={found} written={done} failed={failed} dir={out_dir}", "OK")
        print(f"Fetch {client} ENS JSON ({env}): found={found} written={done} failed={failed}")
        print(f"Output: {out_dir}")
        return 0 if failed == 0 else 1
    except Exception as e:  # noqa: BLE001
        db.log_error("RUN", str(e), type(e).__name__, traceback.format_exc())
        db.finish("ERROR", found, done, max(failed, 1), str(e))
        raise
    finally:
        db.close()


def main() -> int:
    ini_path = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    override = sys.argv[1] if len(sys.argv) > 1 else None
    return run(ini_path, override)


if __name__ == "__main__":
    raise SystemExit(main())
