#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Python database deployment tool.

Applies queued DDL to the database and logs every change to the CHG schema.

Flow:
  1. Read the DB connection from Configuration/Fusion_Flow_QAS.ini ([database]).
  2. Ensure the CHG change-management schema exists (self-bootstrap).
  3. Prompt for a deployment Description (or take --description).
  4. Open one CHG.Deployment row for the run.
  5. For each *.sql in the Queue (filename order): split on GO, execute each
     batch, and on SUCCESS move the script to Archive/<run-stamp>/ and write a
     CHG.Change_Log row. On failure: log FAILED, leave the file in the Queue,
     and stop (unless --continue-on-error).
  6. Finalise CHG.Deployment counts/status. Verbose log -> logs/_Ignore/.

Usage:
  python deploy.py --description "Create CFG foundation"
  python deploy.py --dry-run
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("NO_COLOR", "1")  # ASCII-safe stdout on Windows (Rule 21)

DEPLOY_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEPLOY_DIR.parents[1]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
CHG_BOOTSTRAP = REPO_ROOT / "Configuration" / "SQL" / "000_chg_schema.sql"

GO_SPLIT = re.compile(r"(?im)^[\t ]*GO[\t ]*(?:\d+)?[\t ]*$")


# --------------------------------------------------------------------------- #
# Config / connection
# --------------------------------------------------------------------------- #
def load_db_config(ini_path: Path) -> dict[str, str]:
    if not ini_path.exists():
        raise FileNotFoundError(
            f"Connection file not found: {ini_path}. "
            f"Copy Fusion_Flow_QAS.example.ini to Fusion_Flow_QAS.ini and set the password."
        )
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    if "database" not in parser:
        raise ValueError(f"No [database] section in {ini_path}")
    return {k.lower(): v for k, v in parser["database"].items()}


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


def split_batches(sql: str) -> list[str]:
    """Split a script into batches on lines containing only GO (sqlcmd semantics)."""
    return [b.strip() for b in GO_SPLIT.split(sql) if b.strip()]


# --------------------------------------------------------------------------- #
# Deployment
# --------------------------------------------------------------------------- #
class Deployer:
    def __init__(self, conn: Any, run_stamp: str, log_path: Path, db_cfg: dict[str, str]):
        self.conn = conn
        self.run_stamp = run_stamp
        self.log_path = log_path
        self.db_cfg = db_cfg
        self.deployment_id: int | None = None

    def log(self, message: str, level: str = "INFO") -> None:
        line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}  [{level}]  {message}"
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(line)

    def run_script_text(self, sql: str) -> int:
        """Execute all GO-separated batches of a script in one transaction. Returns batch count."""
        batches = split_batches(sql)
        cur = self.conn.cursor()
        for batch in batches:
            cur.execute(batch)
        self.conn.commit()
        return len(batches)

    def ensure_chg_schema(self) -> None:
        """Self-bootstrap the CHG change-management schema."""
        if CHG_BOOTSTRAP.exists():
            sql = CHG_BOOTSTRAP.read_text(encoding="utf-8-sig")
        else:
            sql = _EMBEDDED_CHG_DDL
        try:
            self.run_script_text(sql)
            self.log("CHG schema ensured.")
        except Exception as error:  # noqa: BLE001
            self.conn.rollback()
            raise RuntimeError(f"Failed to ensure CHG schema: {error}") from error

    def open_deployment(self, description: str, script_count: int) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO CHG.Deployment (RunStamp, Description, ServerName, DatabaseName, AppliedBy, ScriptCount, Status) "
            "OUTPUT INSERTED.DeploymentID VALUES (?, ?, ?, ?, ?, ?, 'RUNNING')",
            self.run_stamp, description[:1000], self.db_cfg.get("server", "")[:200],
            self.db_cfg.get("database", "")[:128], _current_user()[:200], script_count)
        self.deployment_id = int(cur.fetchone()[0])
        self.conn.commit()

    def record_change(self, name: str, script_hash: str, batches: int, status: str,
                      error: str = "", archive_path: str = "") -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO CHG.Change_Log (DeploymentID, ScriptName, ScriptHash, BatchCount, Status, ErrorMessage, ArchivePath) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            self.deployment_id, name[:500], script_hash, batches, status,
            (error[:4000] or None) if error else None, (archive_path[:1000] or None))
        self.conn.commit()

    def finish_deployment(self, succeeded: int, failed: int) -> None:
        status = "COMPLETED" if failed == 0 else "FAILED"
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE CHG.Deployment SET EndedAt = SYSUTCDATETIME(), SucceededCount = ?, "
            "FailedCount = ?, Status = ? WHERE DeploymentID = ?",
            succeeded, failed, status, self.deployment_id)
        self.conn.commit()


def _current_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or getpass.getuser() or "unknown"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def deploy(args: argparse.Namespace) -> int:
    db_cfg = load_db_config(args.ini)
    if args.server:
        db_cfg["server"] = args.server

    queue = args.queue or (DEPLOY_DIR / "Queue")
    archive = args.archive or (REPO_ROOT / "Archive")
    log_root = args.log_root or (REPO_ROOT / "logs")
    ignore_dir = log_root / "_Ignore"
    for d in (queue, archive, log_root, ignore_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Source of DDL: the Queue by default (moved on success), or an explicit
    # --source folder (copied to Archive by default so canonical scripts survive).
    source = args.source or queue
    move_mode = (args.source is None) or args.move

    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = ignore_dir / f"deploy_{run_stamp}.log"

    scripts = sorted(Path(source).glob("*.sql"))
    if not scripts:
        print(f"No *.sql found in {source} - nothing to deploy.")
        if args.source is None:
            print("Tip: stage DDL into the Queue, or deploy a folder directly with "
                  "--source ..\\..\\Configuration\\SQL")
        return 0

    print(f"Queue ({len(scripts)}): " + ", ".join(s.name for s in scripts))
    if args.dry_run:
        print("DRY RUN - no DB changes, no archive, no CHG logging.")
        for s in scripts:
            print(f"  would deploy: {s.name}")
        return 0

    # Description prompt (required for a real run).
    description = args.description
    if not description:
        try:
            description = input("Describe this deployment: ").strip()
        except EOFError:
            description = ""
    if not description:
        print("[ERROR] A deployment description is required (use --description in non-interactive runs).", file=sys.stderr)
        return 2

    import pyodbc  # lazy
    conn = pyodbc.connect(build_connection_string(db_cfg), autocommit=False)
    dep = Deployer(conn, run_stamp, log_path, db_cfg)

    succeeded = failed = 0
    try:
        dep.ensure_chg_schema()
        dep.open_deployment(description, len(scripts))
        dep.log(f"Deployment {run_stamp} (id={dep.deployment_id}) by {_current_user()} "
                f"-> {db_cfg.get('server')}/{db_cfg.get('database')}: {description}")

        run_archive = archive / run_stamp
        for script in scripts:
            text = script.read_text(encoding="utf-8-sig")
            script_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            dep.log(f"--> applying {script.name}")
            try:
                batches = dep.run_script_text(text)
                run_archive.mkdir(parents=True, exist_ok=True)
                dest = run_archive / script.name
                if move_mode:
                    shutil.move(str(script), str(dest))
                else:
                    shutil.copy2(str(script), str(dest))  # keep canonical source intact
                dep.record_change(script.name, script_hash, batches, "SUCCESS", archive_path=str(dest))
                verb = "archived (moved)" if move_mode else "archived (copied)"
                dep.log(f"    SUCCESS ({batches} batch(es)) -> {verb} to {dest}", "OK")
                succeeded += 1
            except Exception as error:  # noqa: BLE001
                conn.rollback()
                failed += 1
                dep.record_change(script.name, script_hash, 0, "FAILED", error=str(error))
                dep.log(f"    FAILED - {error}", "ERROR")
                dep.log("    Script left in Queue for retry.", "ERROR")
                if not args.continue_on_error:
                    break

        dep.finish_deployment(succeeded, failed)
        dep.log(f"Deployment complete: {succeeded} succeeded, {failed} failed.",
                "OK" if failed == 0 else "ERROR")
    finally:
        conn.close()

    if args.promote_log:
        promoted = log_root / f"deploy_{run_stamp}.summary.log"
        shutil.copy2(str(log_path), str(promoted))
        print(f"Promoted log to (committed) logs/: {promoted}")

    return 1 if failed else 0


_EMBEDDED_CHG_DDL = """
IF SCHEMA_ID('CHG') IS NULL EXEC('CREATE SCHEMA CHG');
GO
IF OBJECT_ID('CHG.Deployment','U') IS NULL
CREATE TABLE CHG.Deployment (DeploymentID bigint IDENTITY(1,1) PRIMARY KEY, RunStamp varchar(20) NOT NULL,
 Description nvarchar(1000) NOT NULL, ServerName nvarchar(200) NULL, DatabaseName nvarchar(128) NULL,
 AppliedBy nvarchar(200) NULL, ScriptCount int NOT NULL DEFAULT(0), SucceededCount int NOT NULL DEFAULT(0),
 FailedCount int NOT NULL DEFAULT(0), Status varchar(20) NOT NULL DEFAULT('RUNNING'),
 StartedAt datetime2(3) NOT NULL DEFAULT(SYSUTCDATETIME()), EndedAt datetime2(3) NULL);
GO
IF OBJECT_ID('CHG.Change_Log','U') IS NULL
CREATE TABLE CHG.Change_Log (ChangeID bigint IDENTITY(1,1) PRIMARY KEY, DeploymentID bigint NOT NULL,
 ScriptName nvarchar(500) NOT NULL, ScriptHash char(64) NULL, BatchCount int NULL, Status varchar(20) NOT NULL,
 ErrorMessage nvarchar(max) NULL, ArchivePath nvarchar(1000) NULL,
 AppliedAt datetime2(3) NOT NULL DEFAULT(SYSUTCDATETIME()));
GO
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Fusion Flow V3 QAS - Python DDL deploy tool with CHG change logging.")
    p.add_argument("--description", help="Deployment description (prompted if omitted).")
    p.add_argument("--ini", type=Path, default=DEFAULT_INI, help="Path to Fusion_Flow_QAS.ini")
    p.add_argument("--server", help="Override the server from the .ini")
    p.add_argument("--queue", type=Path, help="Queue folder (default Development/Deploy/Queue)")
    p.add_argument("--source", type=Path, help="Deploy *.sql from this folder instead of the Queue "
                                               "(copied to Archive by default; add --move to move)")
    p.add_argument("--move", action="store_true", help="With --source, move instead of copy to Archive")
    p.add_argument("--archive", type=Path, help="Archive folder (default Archive)")
    p.add_argument("--log-root", type=Path, help="Log root (default logs)")
    p.add_argument("--dry-run", action="store_true", help="List only; no DB changes, no archive, no CHG log")
    p.add_argument("--continue-on-error", action="store_true", help="Keep going after a failing script")
    p.add_argument("--promote-log", action="store_true", help="Copy the run summary up to logs/ (committed)")
    return p


def main() -> int:
    return deploy(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
