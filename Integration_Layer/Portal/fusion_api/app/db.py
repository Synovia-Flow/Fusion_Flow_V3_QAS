from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

from .config import ConfigError, connection_string, db_config, raw_connection_string


class DbUnavailable(RuntimeError):
    pass


def _public_error(error: Exception) -> str:
    text = str(error).splitlines()[0]
    if "password" in text.lower() or "pwd=" in text.lower():
        return "Database connection failed. Check server-side configuration."
    return text[:500]


@contextmanager
def connect():
    try:
        import pyodbc  # type: ignore
    except Exception as exc:  # pragma: no cover - environment dependent
        raise DbUnavailable("pyodbc is not installed in this Python environment.") from exc

    try:
        conn = pyodbc.connect(raw_connection_string() or connection_string(db_config()), autocommit=True, timeout=10)
    except (ConfigError, Exception) as exc:  # noqa: BLE001 - sanitized before leaving API layer
        raise DbUnavailable(_public_error(exc)) from exc

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
