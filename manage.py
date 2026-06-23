#!/usr/bin/env python3
"""
manage.py — Fusion Flow V2 · BKD TSS Portal
Command-line admin tool for running background jobs and DB utilities.

Requires environment variables (or a .env file at repo root):
  AZURE_SQL_SERVER, AZURE_SQL_DATABASE, AZURE_SQL_USER, AZURE_SQL_PASSWORD
  TSS_API_BASE_URL, TSS_API_USERNAME, TSS_API_PASSWORD

Usage:
  python manage.py jobs                     List all available jobs
  python manage.py run <job_name>           Run a specific job
  python manage.py run all                  Run all scheduled cron jobs
  python manage.py run backfill_from_tss    One-time historical backfill from TSS
  python manage.py db check                 Test database connection
  python manage.py db migrate               Apply migration files from migrations/

Examples:
  python manage.py jobs
  python manage.py run poll_ens_status
  python manage.py run sync_sfd
  python manage.py run backfill_from_tss
  python manage.py db check
  python manage.py db migrate
"""

import importlib
import os
import sys
import time

# Ensure repo root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Job registry ─────────────────────────────────────────────
# All modules live in jobs/ (standalone cron scripts).
# discover_sdi, sync_choice_values, backfill_from_tss are thin shims
# that delegate to scripts/ where the full ported logic resides.

JOBS = {
    "poll_ens_status":       "jobs.poll_ens_status",
    "sync_sfd":              "jobs.sync_sfd",
    "poll_sdi_status":       "jobs.poll_sdi_status",
    "discover_sdi":          "jobs.discover_sdi",
    "monitor_sdi_deadlines": "jobs.monitor_sdi_deadlines",
    "sync_choice_values":    "jobs.sync_choice_values",
    "backfill_from_tss":     "jobs.backfill_from_tss",
}

JOB_DESCRIPTIONS = {
    "poll_ens_status":       "Poll submitted consignments for Authorised for Movement / Arrived status",
    "sync_sfd":              "Look up SFD references for authorised consignments",
    "poll_sdi_status":       "Poll submitted SDIs for Closed / Amendment Required / Under Controls status",
    "discover_sdi":          "Discover draft SDIs for arrived goods (delegates to scripts/discover_sdi.py)",
    "monitor_sdi_deadlines": "Flag SDIs approaching the 10th-of-month deadline and send email alerts",
    "sync_choice_values":    "Validate all TSS.CV_* choice value tables (delegates to scripts/sync_choice_values.py)",
    "backfill_from_tss":     "[ONE-TIME] Pull all ENS/SFD/SDI history from TSS Portal (delegates to scripts/backfill_from_tss.py)",
}

# Jobs that run on a cron schedule (excludes one-shot backfill)
CRON_JOBS = [
    "poll_ens_status",
    "sync_sfd",
    "poll_sdi_status",
    "discover_sdi",
    "monitor_sdi_deadlines",
    "sync_choice_values",
]


def _divider(title: str = "") -> None:
    line = "=" * 60
    if title:
        print(f"\n{line}")
        print(f"  {title}")
        print(line)
    else:
        print(line)


def _run_job(name: str) -> bool:
    """Import and execute a single job. Returns True on success."""
    _divider(f"Running: {name}")
    try:
        mod = importlib.import_module(JOBS[name])
        lines = mod.run(triggered_by="manage.py")
        for line in lines:
            print(f"  {line}")
        print(f"\n  Done: {name}")
        return True
    except Exception as exc:
        print(f"\n  ERROR running {name}: {exc}")
        import traceback
        traceback.print_exc()
        return False


# ── Commands ──────────────────────────────────────────────────

def cmd_jobs() -> None:
    """List all available jobs."""
    print("\nAvailable jobs:\n")
    print(f"  {'Name':<35} {'Schedule':<12} Description")
    print(f"  {'-'*35} {'-'*12} {'-'*50}")
    for name, desc in JOB_DESCRIPTIONS.items():
        schedule = "cron" if name in CRON_JOBS else "one-time"
        print(f"  {name:<35} {schedule:<12} {desc}")
    print()
    print("Run a job:   python manage.py run <job_name>")
    print("Run all:     python manage.py run all\n")


def cmd_run(args: list) -> None:
    """Run one or all jobs."""
    if not args:
        print("Error: specify a job name or 'all'.")
        print("  python manage.py run <job_name>")
        print("  python manage.py run all")
        print("  python manage.py jobs  — to list available jobs")
        sys.exit(1)

    target = args[0]

    if target == "all":
        print(f"\nRunning all {len(CRON_JOBS)} cron jobs...")
        failures = []
        for name in CRON_JOBS:
            ok = _run_job(name)
            if not ok:
                failures.append(name)
            time.sleep(1)
        _divider()
        if failures:
            print(f"Completed with errors in: {', '.join(failures)}")
            sys.exit(1)
        else:
            print(f"All {len(CRON_JOBS)} jobs completed successfully.")
    elif target in JOBS:
        ok = _run_job(target)
        if not ok:
            sys.exit(1)
    else:
        print(f"Unknown job: '{target}'")
        print("Run 'python manage.py jobs' to list available jobs.")
        sys.exit(1)


def cmd_db_check() -> None:
    """Test the database connection."""
    print("\nTesting database connection...")
    server   = os.environ.get("AZURE_SQL_SERVER", "(not set)")
    database = os.environ.get("AZURE_SQL_DATABASE", "(not set)")
    user     = os.environ.get("AZURE_SQL_USER") or os.environ.get("AZURE_SQL_USERNAME", "(not set)")
    print(f"  Server:   {server}")
    print(f"  Database: {database}")
    print(f"  User:     {user}")

    from app.db import get_standalone_connection
    try:
        t0 = time.monotonic()
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT @@VERSION AS ver, DB_NAME() AS db, GETUTCDATE() AS utc_now"
        )
        row = cursor.fetchone()
        elapsed = (time.monotonic() - t0) * 1000
        print(f"\n  OK — connected in {elapsed:.0f}ms")
        print(f"  DB:      {row[1]}")
        print(f"  UTC now: {row[2]}")
        print(f"  Engine:  {str(row[0])[:80]}...")

        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'BKD'"
        )
        bkd_tables = cursor.fetchone()[0]
        cursor.execute(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = 'TSS'"
        )
        tss_tables = cursor.fetchone()[0]
        print(f"\n  BKD schema tables: {bkd_tables}")
        print(f"  TSS schema tables: {tss_tables}")
        conn.close()
    except Exception as exc:
        print(f"\n  FAILED: {exc}")
        sys.exit(1)


def cmd_db_migrate() -> None:
    """Apply all migration files from migrations/ directory (idempotent)."""
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    if not os.path.isdir(migrations_dir):
        print(f"Error: {migrations_dir} not found.")
        sys.exit(1)

    sql_files = sorted(
        f for f in os.listdir(migrations_dir)
        if f.endswith(".sql") and not f.startswith("INSTALL") and not f.startswith("hotfix")
    )

    if not sql_files:
        print("No migration files found.")
        return

    print(f"\nApplying {len(sql_files)} migration file(s) from {migrations_dir}...")

    from app.db import get_standalone_connection
    try:
        conn = get_standalone_connection()
        cursor = conn.cursor()
        total_ok = 0
        total_err = 0

        for fname in sql_files:
            path = os.path.join(migrations_dir, fname)
            with open(path, "r", encoding="utf-8") as f:
                sql = f.read()

            batches = [b.strip() for b in sql.split("\nGO") if b.strip()]
            ok = 0
            err = 0
            for batch in batches:
                try:
                    cursor.execute(batch)
                    conn.commit()
                    ok += 1
                except Exception as exc:
                    preview = str(exc).split("\n")[0][:100]
                    # Skip "already exists" type errors silently (idempotent)
                    if "already" in preview.lower() or "duplicate" in preview.lower():
                        ok += 1
                    else:
                        print(f"  [{fname}] batch error: {preview}")
                        err += 1

            status = "OK" if err == 0 else f"{err} error(s)"
            print(f"  {fname}: {ok} batch(es) — {status}")
            total_ok += ok
            total_err += err

        conn.close()
        print(f"\n  Migration complete: {total_ok} batches OK, {total_err} errors.")
        if total_err:
            sys.exit(1)
    except Exception as exc:
        print(f"\n  FAILED to connect: {exc}")
        sys.exit(1)


# ── Entry point ───────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    cmd  = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "jobs":
        cmd_jobs()
    elif cmd == "run":
        cmd_run(args)
    elif cmd == "db":
        if not args:
            print("Usage: python manage.py db [check|migrate]")
            sys.exit(1)
        subcmd = args[0]
        if subcmd == "check":
            cmd_db_check()
        elif subcmd == "migrate":
            cmd_db_migrate()
        else:
            print(f"Unknown db sub-command: '{subcmd}'  (use check or migrate)")
            sys.exit(1)
    else:
        print(f"Unknown command: '{cmd}'\n")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
