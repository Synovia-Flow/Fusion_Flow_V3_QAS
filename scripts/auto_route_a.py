"""
Route A master automation pass.

Runs the end-to-end operator automation sequence in one go:
  ENS validate -> ENS submit -> ENS sync
  cargo validate -> cargo submit -> cargo sync
  auto-stage GMR -> submit GMR -> sync GMR
  SDI autosubmit dry-run/readiness pass

The pass continues through all stages so later sync/discovery steps can still
recover useful state, then exits non-zero if any stage failed.

Usage:
    python scripts/auto_route_a.py
"""
import os
import subprocess
import sys
from datetime import datetime, timezone


SCRIPT_SEQUENCE = [
    ('validate', 'scripts/validate_declarations.py'),
    ('submit', 'scripts/submit_declarations.py'),
    ('sync', 'scripts/sync_statuses.py'),
    ('validate_pipeline', 'scripts/validate_pipeline.py'),
    ('submit_pipeline', 'scripts/submit_pipeline.py'),
    ('sync_pipeline', 'scripts/sync_pipeline.py'),
    ('stage_ready_gmrs', 'scripts/stage_ready_gmrs.py'),
    ('submit_gmr', 'scripts/submit_gmr.py'),
    ('sync_gmr', 'scripts/sync_gmr.py'),
    ('sdi_autosubmit', 'scripts/sdi_autosubmit.py'),
]


def main():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print("Synovia Flow - Route A Automation")
    print("=" * 55)
    print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    failures = []
    completed = 0

    for phase, rel_script in SCRIPT_SEQUENCE:
        abs_script = os.path.join(project_root, rel_script)
        print(f"\n[{completed + 1}/{len(SCRIPT_SEQUENCE)}] {phase} -> {rel_script}")
        run = subprocess.run(
            [sys.executable, abs_script],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        output = (run.stdout or '') + (run.stderr or '')
        if output.strip():
            print(output.rstrip())
        if run.returncode != 0:
            failures.append((phase, run.returncode))
            print(f"[warn] {phase} finished with exit code {run.returncode}")
        completed += 1

    print(f"\n{'=' * 55}")
    print(f"Completed stages: {completed}")
    if failures:
        print("Failures:")
        for phase, code in failures:
            print(f"  - {phase}: exit code {code}")
        sys.exit(1)

    print("Automation pass completed successfully.")
    sys.exit(0)


if __name__ == '__main__':
    main()
