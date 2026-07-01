#!/usr/bin/env python3
"""Fusion Flow V3 QAS - generate liveWeb/.env from the DB connection .ini.

Reads Configuration/Fusion_Flow_QAS.ini [database] and writes liveWeb/.env with the
connection as environment variables, for the blueprint exporter (export_blueprint.py)
and for pasting into Render's environment. The .env holds a password, so it is
gitignored - never commit it.

    python liveWeb/tools/make_env.py            # writes liveWeb/.env
    python liveWeb/tools/make_env.py --print     # also echo a Render env block (password masked)
"""

from __future__ import annotations

import argparse
import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
OUT = REPO_ROOT / "liveWeb" / ".env"

KEYMAP = {
    "server": "DB_SERVER", "database": "DB_NAME", "user": "DB_USER",
    "password": "DB_PASSWORD", "driver": "DB_DRIVER",
    "encrypt": "DB_ENCRYPT", "trust_server_certificate": "DB_TRUST",
}
DEFAULTS = {"DB_DRIVER": "{ODBC Driver 17 for SQL Server}", "DB_ENCRYPT": "yes", "DB_TRUST": "no"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate liveWeb/.env from the DB .ini")
    ap.add_argument("--ini", type=Path, default=DEFAULT_INI)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--print", dest="show", action="store_true", help="Echo a Render env block (password masked)")
    args = ap.parse_args()

    if not args.ini.exists():
        raise SystemExit(f"Connection file not found: {args.ini}. "
                         f"Copy Fusion_Flow_QAS.example.ini to Fusion_Flow_QAS.ini and set the password.")
    cp = configparser.ConfigParser()
    cp.read(args.ini, encoding="utf-8")
    if "database" not in cp:
        raise SystemExit(f"No [database] section in {args.ini}")
    db = {k.lower(): v for k, v in cp["database"].items()}

    env = dict(DEFAULTS)
    for ini_key, env_key in KEYMAP.items():
        if db.get(ini_key) not in (None, ""):
            env[env_key] = db[ini_key]

    lines = ["# Generated from Configuration/Fusion_Flow_QAS.ini by make_env.py.",
             "# Contains a password - DO NOT COMMIT (gitignored).", ""]
    lines += [f"{k}={v}" for k, v in env.items()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}  ({len(env)} vars)")

    if args.show:
        print("\n--- Render environment (Dashboard -> Environment; set DB_PASSWORD as a secret) ---")
        for k, v in env.items():
            print(f"{k}={'********' if k == 'DB_PASSWORD' else v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
