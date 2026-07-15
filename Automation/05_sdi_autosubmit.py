#!/usr/bin/env python3
"""05 - SDI / SupDec discovery, enrichment, validation and guarded submit.

The production behaviour to port lives in:

    app/ingestion/sdi_autosubmit.py
    app/sdi_payloads.py
    app/tss_api.py

Target V3 ownership:

    TSS/SFD evidence -> Processing SDI canonical rows -> Submission update/submit

This script documents the live contract and remains safe until the V3 SDI module
is implemented. Do not use it to submit SDI.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain", action="store_true", help="Show SDI automation contract.")
    parser.add_argument(
        "--allow-placeholder-success",
        action="store_true",
        help="Return success for scheduler wiring tests even though the V3 module is not implemented.",
    )
    args = parser.parse_args()

    print("Automation 05: SDI / SupDec automation")
    print("Required gates:")
    print("- TSS is source of truth for SUP number and SDI goods ids")
    print("- link SDI goods by stable source item/SKU, never description-only")
    print("- validate values, document codes, N935, supplementary units and duplicates")
    print("- obey SDI autosubmit kill switch before any live update/submit")
    if args.explain or args.allow_placeholder_success:
        return 0
    print("Not implemented in V3 Master yet; port from BKD V2 SDI runtime.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
