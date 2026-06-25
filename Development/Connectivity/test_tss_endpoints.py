#!/usr/bin/env python3
"""Fusion Flow V3 QAS - TSS endpoint & credential connectivity test.

For each client/env credential, performs an HTTP Basic authenticated GET against
the TSS API and reports PASS / FAIL with the HTTP status - the same shape as the
connectivity matrix (client_code, env_code, last_status, http_status).

Environments (non-secret base URLs) are hardcoded below. The CREDENTIALS
(usernames + passwords, incl. PRD) are read from a GITIGNORED sidecar file
`tss_credentials.json` beside this script - they are NOT committed to git.
Copy tss_credentials.example.json to tss_credentials.json and fill in the values.

Usage:
  python test_tss_endpoints.py                # test every credential row
  python test_tss_endpoints.py --active-only  # only rows marked active
  python test_tss_endpoints.py --csv results.csv
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# Hardcoded locations (per design): credentials live in Configuration\, results
# are written to the Documentation_Layer share.
CRED_FILE = REPO_ROOT / "Configuration" / "tss_credentials.json"
OUTPUT_DIR = Path(r"\\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Quality\Documentation_Layer")

# Environments - base URLs are public; safe to hardcode.
ENVIRONMENTS = {
    "PRD": {"name": "Production", "base_url": "https://api.tradersupportservice.co.uk/api"},
    "TST": {"name": "Test",       "base_url": "https://api.tsstestenv.co.uk/api"},
}

# Lightweight authenticated read used to validate credentials. Adjust if your
# tenant exposes the choice_values resource under a different path.
TEST_ENDPOINT = "/choice_values/country"
RATE_LIMIT_SECONDS = 0.25
TIMEOUT = 30


def load_credentials() -> list[dict]:
    if not CRED_FILE.exists():
        raise SystemExit(
            f"Missing credentials file: {CRED_FILE}. Copy "
            f"Development\\Connectivity\\tss_credentials.example.json to "
            f"Configuration\\tss_credentials.json and fill in the credentials.")
    data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    return data.get("credentials", data if isinstance(data, list) else [])


def classify(status: int | None, error: str) -> str:
    if status is None:
        return "ERROR"
    if status == 200:
        return "PASS"
    if status in (401, 403):
        return "FAIL (auth)"
    return f"REACHABLE ({status})"


def test_one(cred: dict) -> dict:
    env = ENVIRONMENTS.get((cred.get("env_code") or "").upper())
    result = {
        "client_code": cred.get("client_code"), "env_code": cred.get("env_code"),
        "active": cred.get("active"), "http_status": None, "result": "", "detail": "",
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    }
    if not env:
        result["result"] = "ERROR"; result["detail"] = f"Unknown env_code {cred.get('env_code')}"
        return result
    url = env["base_url"].rstrip("/") + TEST_ENDPOINT
    try:
        resp = requests.get(url, auth=HTTPBasicAuth(cred["username"], cred["password"]),
                            headers={"Accept": "application/json"}, timeout=TIMEOUT)
        result["http_status"] = resp.status_code
        result["result"] = classify(resp.status_code, "")
        result["detail"] = resp.text[:160].replace("\n", " ")
    except requests.RequestException as error:
        result["result"] = "ERROR"; result["detail"] = str(error)[:200]
    return result


def main() -> int:
    p = argparse.ArgumentParser(description="TSS endpoint & credential connectivity test.")
    p.add_argument("--active-only", action="store_true", help="Test only rows marked active")
    p.add_argument("--csv", type=Path, help="Override the results CSV path (default: Documentation_Layer share)")
    args = p.parse_args()

    creds = load_credentials()
    if args.active_only:
        creds = [c for c in creds if str(c.get("active")).lower() in ("1", "true", "yes")]

    print(f"{'CLIENT':<8}{'ENV':<5}{'ACTIVE':<8}{'HTTP':<6}RESULT")
    print("-" * 48)
    results = []
    for i, cred in enumerate(creds):
        if i:
            time.sleep(RATE_LIMIT_SECONDS)
        r = test_one(cred)
        results.append(r)
        print(f"{str(r['client_code']):<8}{str(r['env_code']):<5}{str(r['active']):<8}"
              f"{str(r['http_status'] or '-'):<6}{r['result']}")

    # Always write a timestamped results CSV to the Documentation_Layer share
    # (override with --csv).
    import csv
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = args.csv or (OUTPUT_DIR / f"TSS_Connectivity_{stamp}.csv")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=["client_code", "env_code", "active", "http_status", "result", "detail", "checked_at"])
            w.writeheader(); w.writerows(results)
        print(f"\nResults written to {out_path}")
    except OSError as error:
        print(f"\n[WARN] Could not write results to {out_path}: {error}")

    failures = sum(1 for r in results if r["result"].startswith(("FAIL", "ERROR")))
    print(f"\n{len(results)} tested, {failures} failed/errored.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
