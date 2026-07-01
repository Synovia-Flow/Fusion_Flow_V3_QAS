#!/usr/bin/env python3
"""Fusion Flow V3 QAS - stage pending DDL into the deploy Queue.

The canonical, full back-catalogue of DDL lives in `Configuration/SQL` (committed).
The deploy tool actions whatever is in `Development/Deploy/Queue` and MOVES each
applied script to `Archive/<run-stamp>/`. This helper bridges the two: it copies
the scripts that have NOT yet been applied (per `CHG.Change_Log`) from the canonical
folder into the Queue, so you only ever deploy the individual new/changed DDL.

    Configuration/SQL  --(stage pending)-->  Queue  --(deploy.py)-->  Archive + CHG + DB_Schema.md

"Applied" = a SUCCESS row in CHG.Change_Log whose ScriptHash matches the current
file (same hash the deploy tool computes), so an edited script re-stages while an
unchanged, already-applied one is skipped.

Usage (from Development/Deploy):
    python stage_queue.py                 # stage every un-applied script (asks DB)
    python stage_queue.py --list          # preview only; copy nothing
    python stage_queue.py --only 021,022  # stage just these (offline; no DB needed)
    python stage_queue.py --all           # stage the whole canonical set (offline)
    python stage_queue.py --clear         # remove *.sql from the Queue first, then stage

Then:  python deploy.py --description "..."
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any

DEPLOY_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEPLOY_DIR.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
CANONICAL_SQL = REPO_ROOT / "Configuration" / "SQL"
QUEUE = DEPLOY_DIR / "Queue"


def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Connection file not found: {ini_path}. Copy Fusion_Flow_QAS.example.ini "
            f"to Fusion_Flow_QAS.ini and set the password.")
    cp = configparser.ConfigParser()
    cp.read(ini_path, encoding="utf-8")
    if "database" not in cp:
        raise ValueError(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in cp["database"].items()}


def build_connection_string(db: dict[str, str]) -> str:
    parts = [
        f"Driver={db.get('driver', '{ODBC Driver 17 for SQL Server}')}",
        f"Server={db['server']}",
        f"Database={db['database']}",
    ]
    if db.get("user"):
        parts += [f"Uid={db['user']}", f"Pwd={db.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    yes = lambda v: str(v).lower() in ("yes", "true", "1")
    parts.append(f"Encrypt={'yes' if yes(db.get('encrypt', 'yes')) else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if yes(db.get('trust_server_certificate', 'no')) else 'no'}")
    return ";".join(parts) + ";"


def script_hash(path: Path) -> str:
    """Same hash the deploy tool records: sha256 of the utf-8 text (read utf-8-sig)."""
    text = path.read_text(encoding="utf-8-sig")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def applied_hashes(ini_path: Path) -> set[str] | None:
    """SUCCESS ScriptHashes from CHG.Change_Log, or None if the DB can't be reached."""
    try:
        import pyodbc  # lazy
        conn = pyodbc.connect(build_connection_string(load_db_config(ini_path)), autocommit=True)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] Could not reach the DB to read CHG.Change_Log ({e}).")
        return None
    try:
        cur = conn.cursor()
        if not cur.execute("SELECT OBJECT_ID('CHG.Change_Log','U')").fetchone()[0]:
            return set()
        cur.execute("SELECT ScriptHash FROM CHG.Change_Log WHERE Status = 'SUCCESS' AND ScriptHash IS NOT NULL")
        return {r[0].lower() for r in cur.fetchall() if r[0]}
    finally:
        conn.close()


def _match(name: str, only: list[str]) -> bool:
    """A --only token matches by exact filename or by leading number (e.g. '021')."""
    stem = name.lower()
    for tok in only:
        t = tok.strip().lower()
        if not t:
            continue
        if stem == t or stem == f"{t}.sql" or stem.startswith(f"{t}_"):
            return True
    return False


def stage(args: argparse.Namespace) -> int:
    source = args.source or CANONICAL_SQL
    queue = args.queue or QUEUE
    queue.mkdir(parents=True, exist_ok=True)

    scripts = sorted(Path(source).glob("*.sql"))
    if not scripts:
        print(f"No *.sql found in {source}.")
        return 0

    only = [t for t in (args.only.split(",") if args.only else []) if t.strip()]
    if only:
        scripts = [s for s in scripts if _match(s.name, only)]
        if not scripts:
            print(f"No canonical scripts matched --only {args.only}.")
            return 2

    # Decide what counts as 'already applied'.
    applied: set[str] | None = set()
    if not args.all and not only:
        applied = applied_hashes(args.ini)
        if applied is None:
            print("    Falling back: staging ALL canonical scripts (use --only to narrow, "
                  "or fix the connection to stage only un-applied ones).")
            applied = set()

    if args.clear:
        removed = 0
        for f in queue.glob("*.sql"):
            f.unlink(); removed += 1
        if removed:
            print(f"Cleared {removed} *.sql from the Queue.")

    staged = skipped = 0
    for s in scripts:
        already = (not args.all and not only) and (script_hash(s) in applied)
        if already:
            skipped += 1
            continue
        dest = queue / s.name
        if args.list:
            print(f"  would stage: {s.name}")
        else:
            shutil.copy2(str(s), str(dest))
            print(f"  staged: {s.name}")
        staged += 1

    verb = "would stage" if args.list else "staged"
    print(f"\n{verb}={staged} skipped(already applied)={skipped} -> {queue}")
    if staged and not args.list:
        print('Next: python deploy.py --description "<what this deploys>"')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Stage pending (un-applied) DDL from Configuration/SQL into the deploy Queue.")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI, help="Path to Fusion_Flow_QAS.ini")
    p.add_argument("--source", type=Path, help="Canonical DDL folder (default Configuration/SQL)")
    p.add_argument("--queue", type=Path, help="Queue folder (default Development/Deploy/Queue)")
    p.add_argument("--only", help="Stage only these scripts (comma list of names or leading numbers, e.g. 021,022). Offline.")
    p.add_argument("--all", action="store_true", help="Stage the whole canonical set regardless of CHG. Offline.")
    p.add_argument("--clear", action="store_true", help="Remove *.sql from the Queue before staging.")
    p.add_argument("--list", action="store_true", help="Preview what would be staged; copy nothing.")
    return p


def main() -> int:
    return stage(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
