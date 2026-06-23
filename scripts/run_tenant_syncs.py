"""
Run the TSS sync pipeline for one or more tenants.

This is intended for Render cron. It keeps the sync cadence outside the Flask
web process, while still supporting multi-tenant deployments by setting
TENANT_CODE / CLIENT_CODE for each child run.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PROJECT, "scripts")
RUN_PIPELINE = os.path.join(PROJECT, "scripts", "run_pipeline.py")
SYNC_TSS_TABLES = os.path.join(SCRIPTS, "sync_tss_tables.py")
SYNC_GMR = os.path.join(SCRIPTS, "sync_gmr.py")
SYNC_PRD_ENS = os.path.join(SCRIPTS, "sync_prd_ens_statuses.py")
SYNC_PRD_SDI = os.path.join(SCRIPTS, "sync_prd_sdi_statuses.py")
SDI_AUTOSUBMIT = os.path.join(SCRIPTS, "sdi_autosubmit.py")
LOCK_RESOURCE = "fusion-flow:auto-sync:general-sync"

DEFAULT_SYNC_STEPS = ("prd_ens", "sdi_status", "sdi_autosubmit")
SYNC_STEP_COMMANDS = {
    "prd_ens": (SYNC_PRD_ENS,),
    "sdi_status": (SYNC_PRD_SDI,),
    "sdi_autosubmit": (SDI_AUTOSUBMIT,),
    "legacy_pipeline": (RUN_PIPELINE, "sync"),
    "legacy_tss_tables": (SYNC_TSS_TABLES,),
    "legacy_gmr": (SYNC_GMR,),
}
SYNC_STEP_ALIASES = {
    "prd": "prd_ens",
    "prd_ens": "prd_ens",
    "ens": "prd_ens",
    "status": "prd_ens",
    "statuses": "prd_ens",
    "cargo": "prd_ens",
    "tss": "prd_ens",
    "tss_tables": "prd_ens",
    "sync_tss_tables": "prd_ens",
    "pipeline": "prd_ens",
    "gmr": "prd_ens",
    "gvms": "prd_ens",
    "legacy_pipeline": "legacy_pipeline",
    "legacy_run_pipeline": "legacy_pipeline",
    "legacy_tss_tables": "legacy_tss_tables",
    "legacy_gvms": "legacy_gmr",
    "legacy_gmr": "legacy_gmr",
    "sdi_status": "sdi_status",
    "sdi_statuses": "sdi_status",
    "sdi_sync": "sdi_status",
    "sync_sdi": "sdi_status",
    "supdec_status": "sdi_status",
    "supdec_statuses": "sdi_status",
    "supdec_sync": "sdi_status",
    "sync_supdec": "sdi_status",
    "sdi": "sdi_autosubmit",
    "supdec": "sdi_autosubmit",
    "discover_sdi": "sdi_autosubmit",
    "sdi_discovery": "sdi_autosubmit",
    "sdi_autosubmit": "sdi_autosubmit",
}

try:  # Local convenience only; Render/cron can rely on process env.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT, ".env"))
except Exception:
    pass

sys.path.insert(0, PROJECT)

from app.tenant import TENANT_REGISTRY, normalize_tenant_code  # noqa: E402
from app.db import get_standalone_connection  # noqa: E402
from app import config_store  # noqa: E402


def parse_tenant_codes(raw: str | None) -> list[str]:
    """Return validated tenant codes from comma/space separated config."""
    value = (raw or "").strip()
    if not value:
        return []
    if value.lower() == "all":
        return sorted(TENANT_REGISTRY)

    seen: set[str] = set()
    codes: list[str] = []
    for token in re.split(r"[,;\s]+", value):
        if not token:
            continue
        code = normalize_tenant_code(token)
        if code not in TENANT_REGISTRY:
            raise ValueError(f"Unknown tenant code: {code}")
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def resolve_tenant_codes(cli_value: str | None = None) -> list[str]:
    configured = (
        cli_value
        or os.environ.get("FUSION_AUTO_SYNC_TENANTS")
        or os.environ.get("AUTO_SYNC_TENANTS")
    )
    codes = parse_tenant_codes(configured)
    if codes:
        return codes

    fallback = os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or "BKD"
    return parse_tenant_codes(fallback)


def parse_sync_steps(raw: str | None) -> list[str]:
    """Return the ordered sync steps for the automatic general sync."""
    value = (raw or "").strip()
    if not value or value.lower() == "all":
        return list(DEFAULT_SYNC_STEPS)

    seen: set[str] = set()
    steps: list[str] = []
    for token in re.split(r"[,;\s]+", value):
        if not token:
            continue
        key = token.strip().lower().replace("-", "_")
        step = SYNC_STEP_ALIASES.get(key)
        if not step:
            raise ValueError(f"Unknown automatic sync step: {token}")
        if step not in seen:
            seen.add(step)
            steps.append(step)
    return steps


def resolve_sync_steps(cli_value: str | None = None) -> list[str]:
    configured = cli_value or os.environ.get("FUSION_AUTO_SYNC_STEPS")
    return parse_sync_steps(configured)


def build_child_env(tenant_code: str) -> dict[str, str]:
    tenant = TENANT_REGISTRY[normalize_tenant_code(tenant_code)]
    child_env = os.environ.copy()
    child_env["TENANT_CODE"] = tenant["code"]
    child_env["TENANT_SCHEMA"] = tenant["schema"]
    child_env["CLIENT_CODE"] = tenant["code"]
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    return child_env


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def sdi_tss_autosubmit_enabled(tenant_code: str) -> bool:
    return _truthy(
        config_store.get(
            "SDI_AUTO",
            "SUBMIT_ENABLED",
            fallback="false",
            tenant_code=tenant_code,
        )
    )


def build_step_command(tenant_code: str, step_name: str) -> tuple[str, ...]:
    command = tuple(SYNC_STEP_COMMANDS[step_name])
    if step_name == "sdi_autosubmit" and sdi_tss_autosubmit_enabled(tenant_code):
        return (*command, "--submit", "--no-dry-run")
    return command


@contextmanager
def auto_sync_lock(timeout_ms: int = 0):
    """Hold a SQL Server application lock for this cron invocation."""
    conn = None
    cursor = None
    try:
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SET NOCOUNT ON;
            DECLARE @lock_result INT;
            EXEC @lock_result = sp_getapplock
                @Resource = ?,
                @LockMode = 'Exclusive',
                @LockOwner = 'Session',
                @LockTimeout = ?;
            SELECT @lock_result AS lock_result;
            """,
            [LOCK_RESOURCE, int(timeout_ms)],
        )
        row = cursor.fetchone()
        result = int(row[0]) if row and row[0] is not None else -999
        if result < 0:
            yield False
            return
        yield True
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            if conn is not None:
                conn.close()


def _format_step_command(command: tuple[str, ...]) -> str:
    rel_parts = []
    for part in command:
        if part.startswith(PROJECT):
            rel_parts.append(os.path.relpath(part, PROJECT))
        else:
            rel_parts.append(part)
    return " ".join(rel_parts)


def run_tenant_step(tenant_code: str, step_name: str, timeout_seconds: int) -> bool:
    tenant = TENANT_REGISTRY[normalize_tenant_code(tenant_code)]
    command = build_step_command(tenant["code"], step_name)
    display = _format_step_command(command)
    print(f"[{tenant['code']}] step {step_name}: {display}")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, *command],
            cwd=PROJECT,
            env=build_child_env(tenant["code"]),
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(f"[{tenant['code']}] step {step_name} TIMEOUT after {timeout_seconds}s")
        return False

    elapsed = time.time() - started
    status = "OK" if proc.returncode == 0 else f"FAILED exit={proc.returncode}"
    print(f"[{tenant['code']}] step {step_name} {status} ({elapsed:.1f}s)")
    return proc.returncode == 0


def run_tenant_sync(tenant_code: str, timeout_seconds: int, steps: list[str] | None = None) -> bool:
    tenant = TENANT_REGISTRY[normalize_tenant_code(tenant_code)]
    step_names = steps or list(DEFAULT_SYNC_STEPS)
    print(f"\n[{tenant['code']}] general sync started at {datetime.now(timezone.utc).isoformat()}")
    print(f"[{tenant['code']}] steps: {', '.join(step_names)}")
    started = time.time()
    failures = 0
    for step_name in step_names:
        if not run_tenant_step(tenant["code"], step_name, timeout_seconds):
            failures += 1
    elapsed = time.time() - started
    status = "OK" if failures == 0 else f"FAILED steps={failures}"
    print(f"[{tenant['code']}] general sync {status} ({elapsed:.1f}s)")
    return failures == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run tenant TSS syncs.")
    parser.add_argument(
        "--tenants",
        help="Comma/space separated tenant codes, or 'all'. Defaults to FUSION_AUTO_SYNC_TENANTS, TENANT_CODE, CLIENT_CODE, then BKD.",
    )
    parser.add_argument(
        "--no-lock",
        action="store_true",
        help="Disable SQL Server app lock. Intended only for local troubleshooting.",
    )
    parser.add_argument(
        "--steps",
        help=(
            "Comma/space separated automatic sync steps, or 'all'. "
            "Defaults to FUSION_AUTO_SYNC_STEPS, then all."
        ),
    )
    args = parser.parse_args(argv)

    tenants = resolve_tenant_codes(args.tenants)
    steps = resolve_sync_steps(args.steps)
    timeout_seconds = int(os.environ.get("FUSION_AUTO_SYNC_TIMEOUT_SECONDS", "600"))
    lock_timeout_ms = int(os.environ.get("FUSION_AUTO_SYNC_LOCK_TIMEOUT_MS", "0"))

    print("=" * 61)
    print("Fusion Flow - automatic tenant general sync")
    print(f"Tenants: {', '.join(tenants)}")
    print(f"Steps: {', '.join(steps)}")
    print(f"UTC: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 61)

    if args.no_lock:
        lock_context = nullcontext(True)
    else:
        lock_context = auto_sync_lock(lock_timeout_ms)

    with lock_context as acquired:
        if not acquired:
            print("Another automatic sync is already running; skipping this tick.")
            return 0

        failures = 0
        for tenant_code in tenants:
            try:
                if not run_tenant_sync(tenant_code, timeout_seconds, steps=steps):
                    failures += 1
            except Exception as exc:
                print(f"[{tenant_code}] sync ERROR: {exc}")
                failures += 1

    print("=" * 61)
    print(f"Automatic tenant general sync complete. Failed tenants: {failures}")
    print("=" * 61)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
