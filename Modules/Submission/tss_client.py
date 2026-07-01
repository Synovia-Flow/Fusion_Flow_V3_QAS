#!/usr/bin/env python3
"""Fusion Flow V3 QAS - Module 3 TSS API client.

Thin HTTP client for TSS declaration calls. Resolves the base URL + Basic-auth
credentials from CFG (CFG.TSS_Environment + CFG.TSS_Credential, falling back to the
gitignored tss_credentials.json for the password), enforces the 0.25s rate limit
(Rule 14), and returns a uniform result dict that submission_db.log_call persists.

DRY-RUN: when dry_run=True, call() builds the exact request (url + headers + body)
and returns it WITHOUT contacting TSS - so nothing is sent to HMRC. The Authorization
header is always redacted in the returned/ logged headers.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CRED_FILE = REPO_ROOT / "Configuration" / "tss_credentials.json"
PLACEHOLDER_PWD = "<SET_IN_DB>"
RATE_LIMIT_SECONDS = 0.25          # Rule 14
TIMEOUT = 60


class TssClient:
    def __init__(self, base_url: str, user: str, pwd: str, base_path: str, dry_run: bool):
        self.base_url = base_url.rstrip("/")
        self.base_path = "/" + base_path.strip("/") if base_path else ""
        self.user = user
        self._pwd = pwd
        self.dry_run = dry_run
        self._session = None

    @classmethod
    def from_cfg(cls, db, env_code: str, client_code: str, base_path: str, dry_run: bool) -> "TssClient":
        envs = db.q("SELECT BaseUrl FROM CFG.TSS_Environment WHERE EnvCode = ?", env_code)
        if not envs:
            raise SystemExit(f"No CFG.TSS_Environment row for EnvCode={env_code}")
        base_url = (envs[0]["BaseUrl"] or "").rstrip("/")
        creds = db.q("SELECT TssUsername, TssPassword FROM CFG.TSS_Credential "
                     "WHERE ClientCode = ? AND EnvCode = ?", client_code, env_code)
        if not creds:
            raise SystemExit(f"No CFG.TSS_Credential row for {client_code}/{env_code}")
        user, pwd = creds[0]["TssUsername"], creds[0]["TssPassword"]
        if not pwd or pwd == PLACEHOLDER_PWD:
            pwd = _password_from_json(client_code, env_code)
        if not pwd and not dry_run:
            raise SystemExit(f"No usable TSS password for {client_code}/{env_code} "
                             f"(set it in CFG.TSS_Credential or {CRED_FILE}).")
        return cls(base_url, user, pwd or "", base_path, dry_run)

    def url_for(self, endpoint: str) -> str:
        return f"{self.base_url}{self.base_path}/{endpoint.strip('/')}"

    def call(self, method: str, endpoint: str, body: dict | None = None) -> dict:
        url = self.url_for(endpoint)
        redacted = {"Authorization": "Basic ***", "Content-Type": "application/json",
                    "Accept": "application/json"}
        result: dict[str, Any] = {
            "method": method.upper(), "request_url": url, "request_headers": redacted,
            "request_json": body, "response_headers": None, "response_text": None,
            "status_code": None, "ok": False, "duration_ms": None, "is_dry_run": self.dry_run,
            "error": None,
        }
        if self.dry_run:
            result["ok"] = True            # request was built successfully; nothing sent
            result["response_text"] = json.dumps({"dry_run": True, "note": "not sent to TSS"})
            return result
        try:
            import requests
            from requests.auth import HTTPBasicAuth
            if self._session is None:
                self._session = requests.Session()
                self._session.auth = HTTPBasicAuth(self.user, self._pwd)
            time.sleep(RATE_LIMIT_SECONDS)
            t0 = time.monotonic()
            resp = self._session.request(method.upper(), url, json=body, timeout=TIMEOUT,
                                         headers={"Accept": "application/json"})
            result["duration_ms"] = int((time.monotonic() - t0) * 1000)
            result["status_code"] = resp.status_code
            result["ok"] = resp.ok
            result["response_text"] = resp.text
            result["response_headers"] = dict(resp.headers)
            if not resp.ok:
                result["error"] = f"HTTP {resp.status_code}"
        except Exception as e:  # noqa: BLE001
            result["error"] = str(e)
        return result

    @staticmethod
    def parse_json(result: dict) -> Any:
        txt = result.get("response_text")
        if not txt:
            return None
        try:
            return json.loads(txt)
        except (ValueError, TypeError):
            return None


def _password_from_json(client_code: str, env_code: str) -> str | None:
    if not CRED_FILE.exists():
        return None
    try:
        data = json.loads(CRED_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    rows = data.get("credentials", data if isinstance(data, list) else [])
    for c in rows:
        if (str(c.get("client_code")).upper() == client_code.upper()
                and str(c.get("env_code")).upper() == env_code.upper()):
            return c.get("password")
    return None
