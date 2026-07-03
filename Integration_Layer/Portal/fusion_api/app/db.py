from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .config import ConfigError, connection_string, db_config, raw_connection_string


class DbUnavailable(RuntimeError):
    pass


ODBC_DRIVER_ERROR_MARKERS = (
    "can't open lib",
    "data source name not found",
    "driver manager",
    "odbc driver",
)


def _public_error(error: Exception) -> str:
    text = str(error).splitlines()[0]
    if "password" in text.lower() or "pwd=" in text.lower():
        return "Database connection failed. Check server-side configuration."
    return text[:500]


def _parse_connection_string(value: str) -> dict[str, str]:
    parts: list[str] = []
    token: list[str] = []
    brace_depth = 0
    for char in value:
        if char == "{":
            brace_depth += 1
        elif char == "}" and brace_depth:
            brace_depth -= 1
        if char == ";" and brace_depth == 0:
            part = "".join(token).strip()
            if part:
                parts.append(part)
            token = []
            continue
        token.append(char)
    final = "".join(token).strip()
    if final:
        parts.append(final)

    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, part_value = part.split("=", 1)
        parsed[key.strip().lower()] = part_value.strip().strip("{}")
    return parsed


def _connection_options() -> dict[str, str]:
    raw = raw_connection_string()
    if raw:
        return _parse_connection_string(raw)

    config = db_config()
    options = {
        "server": config["server"],
        "database": config["database"],
        "encrypt": config.get("encrypt", "yes"),
        "trustservercertificate": config.get("trust_server_certificate", "no"),
    }
    if config.get("user"):
        options["uid"] = config["user"]
        options["pwd"] = config.get("password", "")
    return options


def _option(options: dict[str, str], *names: str) -> str:
    for name in names:
        value = options.get(name.lower())
        if value:
            return value
    return ""


def _server_and_port(value: str) -> tuple[str, int | None]:
    server = value.strip()
    if server.lower().startswith("tcp:"):
        server = server[4:]
    if "," not in server:
        return server, None
    host, port_text = server.rsplit(",", 1)
    try:
        return host.strip(), int(port_text.strip())
    except ValueError:
        return server, None


def _should_try_pytds(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in ODBC_DRIVER_ERROR_MARKERS)


def _qmark_to_pyformat(sql: str) -> str:
    converted: list[str] = []
    in_single_quote = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            converted.append(char)
            if in_single_quote and index + 1 < len(sql) and sql[index + 1] == "'":
                converted.append(sql[index + 1])
                index += 2
                continue
            in_single_quote = not in_single_quote
        elif char == "?" and not in_single_quote:
            converted.append("%s")
        else:
            converted.append(char)
        index += 1
    return "".join(converted)


class _PytdsCursor:
    def __init__(self, cursor: Any):
        self._cursor = cursor

    @property
    def description(self) -> Any:
        return self._cursor.description

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0) or 0)

    def execute(self, sql: str, params: Iterable[Any] = ()) -> "_PytdsCursor":
        self._cursor.execute(_qmark_to_pyformat(sql), tuple(params))
        return self

    def fetchall(self) -> Any:
        return self._cursor.fetchall()

    def fetchone(self) -> Any:
        return self._cursor.fetchone()


class _PytdsConnection:
    def __init__(self, connection: Any):
        self._connection = connection

    def cursor(self) -> _PytdsCursor:
        return _PytdsCursor(self._connection.cursor())

    def close(self) -> None:
        self._connection.close()


def _connect_pytds() -> _PytdsConnection:
    try:
        import certifi  # type: ignore
        import pytds  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise DbUnavailable("python-tds fallback is not installed in this Python environment.") from exc

    options = _connection_options()
    server_raw = _option(options, "server", "addr", "address", "network address")
    database = _option(options, "database", "initial catalog")
    user = _option(options, "uid", "user id", "user")
    password = _option(options, "pwd", "password")
    if not server_raw or not database:
        raise DbUnavailable("Database server or database name is missing from server-side configuration.")
    if not user:
        raise DbUnavailable("python-tds fallback requires SQL authentication.")

    server, port = _server_and_port(server_raw)
    connection = pytds.connect(
        server=server,
        port=port or 1433,
        database=database,
        user=user,
        password=password,
        timeout=10,
        login_timeout=10,
        autocommit=True,
        appname="Fusion Flow Portal API",
        cafile=certifi.where(),
        validate_host=True,
    )
    return _PytdsConnection(connection)


@contextmanager
def connect():
    try:
        import pyodbc  # type: ignore
    except Exception:  # pragma: no cover - environment dependent
        pyodbc_error: Exception = DbUnavailable("pyodbc is not installed in this Python environment.")
    else:
        try:
            conn = pyodbc.connect(raw_connection_string() or connection_string(db_config()), autocommit=True, timeout=10)
        except ConfigError as exc:
            raise DbUnavailable(_public_error(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - sanitized before leaving API layer
            if not _should_try_pytds(exc):
                raise DbUnavailable(_public_error(exc)) from exc
            pyodbc_error = exc
        else:
            try:
                yield conn
            finally:
                conn.close()
            return

    try:
        conn = _connect_pytds()
    except Exception as exc:  # noqa: BLE001 - sanitized before leaving API layer
        raise DbUnavailable(f"{_public_error(pyodbc_error)}; python-tds fallback failed: {_public_error(exc)}") from exc

    try:
        yield conn
    finally:
        conn.close()


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def rows_to_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [
        {columns[index]: to_jsonable(value) for index, value in enumerate(row)}
        for row in cursor.fetchall()
    ]


def query_all(sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return rows_to_dicts(cursor)


def query_one(sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
    rows = query_all(sql, params)
    return rows[0] if rows else None


def execute_scalar(sql: str, params: Iterable[Any] = ()) -> Any:
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        row = cursor.fetchone()
        return to_jsonable(row[0]) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with connect() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return int(cursor.rowcount or 0)
