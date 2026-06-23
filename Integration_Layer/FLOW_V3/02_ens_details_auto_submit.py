#!/usr/bin/env python3
"""02 - ENS DETAILS auto-submit placeholder.

This QAS repository is being rebuilt from zero. The V2 implementation is the
reference, but it is not imported here. Implement the local ENS DETAILS service
before enabling this step.
"""

from __future__ import annotations

import argparse
import os


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-code", default=os.environ.get("TENANT_CODE") or "BKD")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--header-id", type=int, action="append", default=[])
    args = parser.parse_args()

    tenant_code = str(args.tenant_code or "BKD").strip().upper()
    print(f"FLOW V3 02 -> ENS DETAILS auto-submit tenant={tenant_code}")
    print("Pending local QAS implementation: app/services/ens_details.py")
    print("No external FUSION_FLOW_APP_ROOT dependency is used.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
