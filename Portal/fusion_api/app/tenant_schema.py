"""Tenant-to-schema resolution for the Fusion_Flow_V3 mirror database.

In that database the tenant IS the schema: BKD, PLE, CWF and CRS each own their
own Consignments, ENS_Headers, GoodsItems and (except CRS) SFD_Declarations /
SDI_Declarations. There is no ClientCode column to filter on, so the schema name
has to be interpolated into the SQL text - which is why it may only ever come
from the allowlist below, never from caller input.

The schemas are near-identical but not identical, and the differences are real:

    CWF   no consignment_id, no loaded_at; has created_at instead
    CRS   no goods_checked_at; no SFD_Declarations / SDI_Declarations at all

So anything that reads those columns has to ask first. `mirror_has_column` does
that against INFORMATION_SCHEMA and caches the answer for the process.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .db import DbUnavailable, mirror_query_all

# Portal client code -> mirror schema. CWD is the portal's code for Countrywide,
# whose data and TSS credentials both live under CWF; the same alias already
# exists in the TSS credential bridge.
TENANT_SCHEMAS: dict[str, str] = {
    "BKD": "BKD",
    "PLE": "PLE",
    "CWD": "CWF",
    "CWF": "CWF",
    "CRS": "CRS",
}

# Belt and braces: even an allowlisted value is checked before it reaches SQL.
_SAFE_SCHEMA = re.compile(r"^[A-Z]{3}$")

# Tenants that carry the downstream declaration tables at all.
TENANTS_WITH_SFD = frozenset({"BKD", "PLE", "CWF"})


class UnknownTenant(ValueError):
    pass


def tenant_schema(client_code: object) -> str:
    """Resolve a portal client code to its mirror schema, or raise UnknownTenant."""
    code = str(client_code or "").strip().upper()
    schema = TENANT_SCHEMAS.get(code)
    if not schema or not _SAFE_SCHEMA.match(schema):
        raise UnknownTenant(f"'{code}' is not a known tenant of the mirror database.")
    return schema


def known_tenants() -> list[str]:
    return sorted(TENANT_SCHEMAS)


@lru_cache(maxsize=1)
def _mirror_columns() -> frozenset[tuple[str, str, str]]:
    rows = mirror_query_all(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA IN ('BKD', 'PLE', 'CWF', 'CRS')
        """
    )
    return frozenset(
        (str(row["TABLE_SCHEMA"]).upper(), str(row["TABLE_NAME"]).upper(), str(row["COLUMN_NAME"]).upper())
        for row in rows
    )


def mirror_has_column(schema: str, table: str, column: str) -> bool:
    """True when this tenant's copy of the table carries the column."""
    try:
        return (schema.upper(), table.upper(), column.upper()) in _mirror_columns()
    except DbUnavailable:
        raise
    except Exception:
        return False


def tenant_has_sfd(schema: str) -> bool:
    return schema.upper() in TENANTS_WITH_SFD


def first_present_column(schema: str, table: str, alias: str, names: tuple[str, ...], null_cast: str) -> str:
    """SQL for the first of `names` this tenant actually has, else a typed NULL.

    Absorbs the drift between tenant schemas in one place. Where more than one of
    the candidates exists they are COALESCEd in the order given, so the preferred
    name wins without having to know which tenant is which.
    """
    present = [f"{alias}.{name}" for name in names if mirror_has_column(schema, table, name)]
    if not present:
        return f"CAST(NULL AS {null_cast})"
    if len(present) == 1:
        return present[0]
    return f"COALESCE({', '.join(present)})"


def consignment_id_expression(schema: str) -> str:
    """The numeric consignment id, or NULL for tenants that do not keep one.

    CWF has no consignment_id: its Consignments PK is consignment_number, which
    is the TSS DEC reference and is the only key present in every tenant.
    """
    return first_present_column(schema, "Consignments", "c", ("consignment_id",), "int")


def consignment_loaded_at_expression(schema: str) -> str:
    """When the row landed. CWF names it created_at; BKD/PLE/CRS name it loaded_at."""
    return first_present_column(schema, "Consignments", "c", ("loaded_at", "created_at"), "datetime2")


def ens_header_id_expression(schema: str) -> str:
    """The numeric header id. CWF keeps none; its ENS_Headers PK is declaration_number."""
    return first_present_column(schema, "ENS_Headers", "h", ("header_id",), "int")


def ens_header_loaded_at_expression(schema: str) -> str:
    return first_present_column(schema, "ENS_Headers", "h", ("loaded_at", "created_at"), "datetime2")
