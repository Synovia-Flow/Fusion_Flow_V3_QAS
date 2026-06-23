"""Shared helpers for resolving SQL Server ODBC connection strings."""
import os
import platform

import pyodbc


DEFAULT_DATABASE = 'Fusion_TSS'
DEFAULT_TIMEOUT = 30
ODBC_DRIVER_CANDIDATES = ('ODBC Driver 18 for SQL Server', 'ODBC Driver 17 for SQL Server')


def _odbcinst_ini_paths():
    paths = []
    sysini_dir = os.environ.get('ODBCSYSINI')
    instini_name = os.environ.get('ODBCINSTINI')

    if sysini_dir:
        paths.append(os.path.join(sysini_dir, instini_name or 'odbcinst.ini'))
    elif instini_name and os.path.isabs(instini_name):
        paths.append(instini_name)

    paths.extend(('/etc/odbcinst.ini', '/usr/local/etc/odbcinst.ini'))

    seen = set()
    unique_paths = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique_paths.append(path)
    return unique_paths


def _drivers_from_odbcinst():
    drivers = set()
    for path in _odbcinst_ini_paths():
        try:
            with open(path, encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith('[') and line.endswith(']'):
                        drivers.add(line[1:-1])
        except OSError:
            continue
    return drivers


def detect_odbc_driver():
    """Return a sensible SQL Server ODBC driver, preferring explicit config."""
    configured_driver = os.environ.get('DB_DRIVER')
    if configured_driver:
        return configured_driver

    try:
        available = set(pyodbc.drivers())
    except Exception:
        available = set()

    if not available:
        available = _drivers_from_odbcinst()

    for candidate in ODBC_DRIVER_CANDIDATES:
        if candidate in available:
            return f'{{{candidate}}}'

    if platform.system() == 'Windows':
        return '{ODBC Driver 17 for SQL Server}'
    return '{ODBC Driver 17 for SQL Server}'


def _read_value(config, key):
    if config is None:
        return None
    if hasattr(config, 'get'):
        return config.get(key)
    return getattr(config, key, None)


def _lookup(config, *keys, default=''):
    for key in keys:
        value = _read_value(config, key)
        if value not in (None, ''):
            return str(value).strip()

    for key in keys:
        value = os.environ.get(key)
        if value not in (None, ''):
            return value.strip()

    return default


def normalize_connection_string(connection_string):
    connection_string = (connection_string or '').strip()
    if not connection_string:
        return ''
    if not connection_string.endswith(';'):
        connection_string += ';'
    return connection_string


def build_connection_string(config=None, timeout=DEFAULT_TIMEOUT, include_retry=False):
    """Return DB_CONN_STR when present, otherwise build a compatible fallback."""
    raw_connection_string = _lookup(config, 'DB_CONN_STR') or os.environ.get('ODBC_CONNECTION_STRING', '').strip()
    if raw_connection_string:
        return normalize_connection_string(raw_connection_string)

    driver = _lookup(config, 'DB_DRIVER', 'ODBC_DRIVER', default=detect_odbc_driver())
    server = _lookup(config, 'AZURE_SQL_SERVER', 'DB_SERVER')
    database = _lookup(config, 'AZURE_SQL_DATABASE', 'DB_NAME', default=DEFAULT_DATABASE)
    username = _lookup(config, 'AZURE_SQL_USERNAME', 'DB_USER')
    password = _lookup(config, 'AZURE_SQL_PASSWORD', 'DB_PASSWORD')

    parts = [
        f'DRIVER={driver}',
        f'SERVER={server}',
        f'DATABASE={database}',
        f'UID={username}',
        f'PWD={password}',
        'Encrypt=yes',
        'TrustServerCertificate=no',
        f'Connection Timeout={int(timeout)}',
    ]

    if include_retry:
        parts.extend([
            'ConnectRetryCount=3',
            'ConnectRetryInterval=10',
        ])

    return ';'.join(parts) + ';'
