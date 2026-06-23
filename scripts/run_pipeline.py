"""
NOT FOR PRD: reads/writes legacy BKD.Staging* tables removed by migration 078. Do not run against Fusion_TSS_Automation_PRD.

Synovia Flow - Master Pipeline Runner
Runs validation, submission, and sync scripts in sequence.

Usage:
    python scripts/run_pipeline.py
    python scripts/run_pipeline.py validate
    python scripts/run_pipeline.py submit
    python scripts/run_pipeline.py sync
"""
import os
import subprocess
import sys
import time
from datetime import datetime

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(PROJECT, 'scripts')

PIPELINE = [
    ('VALIDATE', [
        ('ENS Headers', 'validate_declarations.py'),
        ('Pipeline (Cons/Goods/SDI)', 'validate_pipeline.py'),
    ]),
    ('SUBMIT', [
        ('ENS Headers', 'submit_declarations.py'),
        ('Pipeline (Cons/Goods/SDI)', 'submit_pipeline.py'),
    ]),
    ('SYNC', [
        ('ENS Headers', 'sync_statuses.py'),
        ('Pipeline (Cons/Goods/SDI)', 'sync_pipeline.py'),
    ]),
]


def resolve_python():
    override = os.environ.get('FUSION_PYTHON') or os.environ.get('PYTHON_EXECUTABLE')
    candidates = [
        override,
        os.path.join(PROJECT, '.venv', 'Scripts', 'python.exe'),
        os.path.join(PROJECT, 'venv', 'Scripts', 'python.exe'),
        sys.executable,
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return sys.executable or 'python'


PYTHON = resolve_python()


def run_script(name, script_file):
    path = os.path.join(SCRIPTS, script_file)
    if not os.path.exists(path):
        print(f"    SKIP: {script_file} not found")
        return False

    print(f"\n  Running: {name} ({script_file})")
    print(f"  {'-' * 50}")

    t0 = time.time()
    try:
        result = subprocess.run(
            [PYTHON, path],
            cwd=PROJECT,
            capture_output=False,
            timeout=300,
        )
        elapsed = time.time() - t0
        status = 'OK' if result.returncode == 0 else f'EXIT {result.returncode}'
        print(f"  {'-' * 50}")
        print(f"  {status} ({elapsed:.1f}s)")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("  TIMEOUT after 300s")
        return False
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return False


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else 'all'

    print(
        "\n".join([
            "=" * 61,
            "  Synovia Flow - Master Pipeline Runner",
            f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"  Mode: {mode}",
            "=" * 61,
        ])
    )

    if mode == 'all':
        phases_to_run = ['VALIDATE', 'SUBMIT', 'SYNC']
    elif mode in ('validate', 'val'):
        phases_to_run = ['VALIDATE']
    elif mode in ('submit', 'sub'):
        phases_to_run = ['SUBMIT']
    elif mode in ('sync', 'poll'):
        phases_to_run = ['SYNC']
    else:
        print(f"Unknown mode: {mode}")
        print("Usage: run_pipeline.py [all|validate|submit|sync]")
        sys.exit(1)

    total_ok = 0
    total_fail = 0

    for phase_name, scripts in PIPELINE:
        if phase_name not in phases_to_run:
            continue

        print(f"\n{'=' * 55}")
        print(f"  PHASE: {phase_name}")
        print(f"{'=' * 55}")

        for name, script_file in scripts:
            success = run_script(name, script_file)
            if success:
                total_ok += 1
            else:
                total_fail += 1

    print(
        "\n".join([
            "",
            "=" * 55,
            "  PIPELINE COMPLETE",
            f"  Scripts OK: {total_ok}  |  Failed: {total_fail}",
            "=" * 55,
            "",
        ])
    )
    sys.exit(1 if total_fail > 0 else 0)


if __name__ == '__main__':
    main()
