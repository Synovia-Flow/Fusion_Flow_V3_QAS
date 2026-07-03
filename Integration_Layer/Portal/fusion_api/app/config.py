from __future__ import annotations

import configparser
import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_INI = REPO_ROOT / "Configuration" / "Fusion_Flow_QAS.ini"
DEFAULT_DOTENV = REPO_ROOT / ".env"


class ConfigError(RuntimeError):
    pass


def _clean_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _dotenv_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = _clean_env_value(value)
    return values


@lru_cache(maxsize=1)
def raw_connection_string() -> str:
    env_value = os.environ.get("DB_CONN_STR")
    if env_value:
        return _clean_env_value(env_value)
    return _dotenv_values(DEFAULT_DOTENV).get("DB_CONN_STR", "")


@lru_cache(maxsize=1)
def db_config() -> dict[str, str]:
    ini_path = Path(os.environ.get("FUSION_FLOW_INI", str(DEFAULT_INI)))
    if not ini_path.exists():
        raise ConfigError(
            "Database connection file is not available. Set DB_CONN_STR, FUSION_FLOW_INI, or create Configuration/Fusion_Flow_QAS.ini."
        )
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    if "database" not in parser:
        raise ConfigError("Database connection file does not contain a [database] section.")
    return {key.lower(): value for key, value in parser["database"].items()}


def connection_string(config: dict[str, str]) -> str:
    parts = [
        f"Driver={config.get('driver', '{ODBC Driver 17 for SQL Server}')}",
        f"Server={config['server']}",
        f"Database={config['database']}",
    ]
    if config.get("user"):
        parts += [f"Uid={config['user']}", f"Pwd={config.get('password', '')}"]
    else:
        parts.append("Trusted_Connection=yes")
    encrypt = config.get("encrypt", "yes").lower() in {"yes", "true", "1"}
    trust = config.get("trust_server_certificate", "no").lower() in {"yes", "true", "1"}
    parts.append(f"Encrypt={'yes' if encrypt else 'no'}")
    parts.append(f"TrustServerCertificate={'yes' if trust else 'no'}")
    return ";".join(parts) + ";"



def config_value(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    if value is not None:
        return _clean_env_value(value)
    return _dotenv_values(DEFAULT_DOTENV).get(key, default)


def allowed_origins() -> list[str]:
    raw = os.environ.get("FUSION_PORTAL_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]
