#!/usr/bin/env python3
"""Fusion Flow V3 QAS - reprocess job.

Re-runs already-processed records through the SAME config-driven engine in
REPROCESS mode (the normal engine only picks up untracked rows). Used after a
config/data fix - e.g. seeding a carrier, refreshing choice values, or correcting
the field map - to re-evaluate previously REJECTED movements and CLOSE OFF their
errors: a record that now passes is updated in place to VALIDATED, its
RejectReason cleared, and the tracking row stamped ResolvedAt /
ResolvedByExecutionID (so it leaves the error views).

No CLI. Controls from CFG.Application_Parameters:
    PROCESSING_CLIENT            client to reprocess        (default BKD)
    PROCESSING_ENTITY            entity                     (default ENS_HEADER)
    PROCESSING_REPROCESS_SCOPE   REJECTED (default) | ALL
    PROCESSING_MOVEMENT_KEY      optional - reprocess a single movement
    PROCESSING_DRY_RUN           1/true = report only
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    from .process_engine import run, DEFAULT_INI
except Exception:  # pragma: no cover - script context
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from process_engine import run, DEFAULT_INI  # type: ignore


def main() -> int:
    ini = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    return run(ini, mode="REPROCESS")


if __name__ == "__main__":
    raise SystemExit(main())
