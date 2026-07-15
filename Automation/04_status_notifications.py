#!/usr/bin/env python3
"""04 - Status watcher and notification gate.

This is the V3 automation contract for:

    TSS status sync -> failure notifications -> Authorised for Movement
    -> customer ENS Movement Pack

V3 Master does not yet contain the full notification module. The production
behaviour to port lives in the BKD V2 runtime:

    app/ingestion/ens_status_watcher.py
    app/ingestion/automation_notify.py
    app/templates/declarations/_email_pack_body.html

Until that module is ported, this script is intentionally explain-only unless
called with --allow-placeholder-success.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain", action="store_true", help="Show notification contract.")
    parser.add_argument(
        "--allow-placeholder-success",
        action="store_true",
        help="Return success for scheduler wiring tests even though the V3 module is not implemented.",
    )
    args = parser.parse_args()

    print("Automation 04: status watcher + notifications")
    print("Required gates:")
    print("- use official TSS status, not local sub_status")
    print("- all active DEC consignments authorised before final movement email")
    print("- no goods blockers remain")
    print("- STG.BKD_ENS_Headers.movement_notified_at must be null before sending")
    print("- ENS Movement Pack gross weights display with two decimal places")
    if args.explain or args.allow_placeholder_success:
        return 0
    print("Not implemented in V3 Master yet; port from BKD V2 notification runtime.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
