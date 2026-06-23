#!/usr/bin/env python3
"""05 - SDI/SupDec status sync and autosubmit.

Runs the tenant SDI sync path: discover SUP records exposed by TSS, enrich from
source/masterdata, update goods, update header, submit when the SDI_AUTO submit
toggle is enabled, then re-read TSS status for official outcome.

Set FUSION_FLOW_APP_ROOT when this QAS folder does not contain the Flask app.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _app_root() -> Path:
    candidate = Path(os.environ.get("FUSION_FLOW_APP_ROOT") or Path(__file__).resolve().parents[2])
    if not (candidate / "scripts" / "run_tenant_syncs.py").exists():
        raise SystemExit(
            "Fusion app root not found. Set FUSION_FLOW_APP_ROOT to the repo that contains scripts/run_tenant_syncs.py."
        )
    return candidate


ROOT = _app_root()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-code", default=os.environ.get("TENANT_CODE") or "BKD")
    parser.add_argument("--no-lock", action="store_true")
    args = parser.parse_args()

    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_tenant_syncs.py"),
        "--tenants",
        args.tenant_code,
        "--steps",
        "sdi_status,sdi_autosubmit",
    ]
    if args.no_lock:
        command.append("--no-lock")

    print("FLOW V3 05 -> SDI status sync + autosubmit")
    print(" ".join(command))
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
