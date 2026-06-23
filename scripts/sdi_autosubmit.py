#!/usr/bin/env python3
"""Run the PRD-safe SDI/SupDec auto-submit worker."""

from __future__ import annotations

import argparse
import json
import os
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

try:  # Local convenience only; production can use process env/AppConfiguration.
    from dotenv import load_dotenv

    load_dotenv(os.path.join(PROJECT, ".env"))
except Exception:
    pass

from app.ingestion.sdi_autosubmit import run_sdi_autosubmit  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tenant-code",
        default=os.environ.get("TENANT_CODE") or os.environ.get("CLIENT_CODE") or "BKD",
        help="Tenant code to process. Defaults to TENANT_CODE, CLIENT_CODE, then BKD",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum SFD candidates to process. Defaults to SDI_AUTO.MAX_ITEMS.",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Request TSS update+submit. Requires --no-dry-run and SDI_AUTO.SUBMIT_ENABLED=true.",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Allow live TSS update/submit calls when --submit and config permit it.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_sdi_autosubmit(
        tenant_code=args.tenant_code,
        dry_run=not args.no_dry_run,
        submit=args.submit,
        limit=args.limit,
    )
    summary = {
        "tenant_code": result.tenant_code,
        "dry_run": result.dry_run,
        "submit_requested": result.submit_requested,
        "submit_enabled": result.submit_enabled,
        "effective_submit": result.effective_submit,
        "candidates": result.candidates,
        "discovered": result.discovered,
        "staged_headers": result.staged_headers,
        "staged_goods": result.staged_goods,
        "ready": result.ready,
        "blocked": result.blocked,
        "submitted": result.submitted,
        "errors": result.errors[:20],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
