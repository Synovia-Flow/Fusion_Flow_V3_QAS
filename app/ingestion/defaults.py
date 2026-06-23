from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os

from app.db import get_standalone_connection
from app.tenant import get_tenant, get_tenant_by_code


def _resolve_schema(tenant_code: str | None = None) -> str:
    if tenant_code:
        try:
            return get_tenant_by_code(tenant_code)["schema"]
        except KeyError:
            pass
    return get_tenant()["schema"]


def _load_category_values(category: str, tenant_code: str | None = None) -> dict[str, str]:
    schema = _resolve_schema(tenant_code)
    conn = None
    cursor = None
    try:
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT config_key, config_value
            FROM [{schema}].AppConfiguration
            WHERE category = ?
            """,
            [category],
        )
        return {
            row[0]: (row[1] or "")
            for row in cursor.fetchall()
        }
    except Exception:
        return {}
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def _load_company_values(tenant_code: str | None = None) -> dict[str, str]:
    schema = _resolve_schema(tenant_code)
    conn = None
    cursor = None
    try:
        conn = get_standalone_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT TOP 1
                company_name, trading_name, eori_xi, eori_gb,
                address_line1, city, postcode, country
            FROM [{schema}].CompanyMaster
            ORDER BY id
            """
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return {
            "company_name": row[0] or "",
            "trading_name": row[1] or "",
            "eori_xi": row[2] or "",
            "eori_gb": row[3] or "",
            "address_line1": row[4] or "",
            "city": row[5] or "",
            "postcode": row[6] or "",
            "country": row[7] or "",
        }
    except Exception:
        return {}
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def _cfg(values: dict[str, str], category: str, key: str, fallback: str = '') -> str:
    raw_value = str(values.get(key) or '').strip()
    if raw_value:
        return raw_value

    env_key = f'{category}_{key}'
    env_value = os.environ.get(env_key)
    if env_value not in (None, ''):
        return env_value

    return fallback


def _cfg_env_override(values: dict[str, str], category: str, key: str, fallback: str = '') -> str:
    """Return env var first, then AppConfiguration.

    Used for operational kill-switch style flags where a Render cron env var
    must be able to override seeded tenant defaults.
    """
    env_key = f'{category}_{key}'
    env_value = os.environ.get(env_key)
    if env_value not in (None, ''):
        return env_value
    return _cfg(values, category, key, fallback)


def _as_bool(value: str, default: bool = False) -> bool:
    text = (value or '').strip().lower()
    if not text:
        return default
    return text in {'1', 'true', 'yes', 'y', 'on'}


def _as_int(value: str, default: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


@dataclass
class IngestDefaults:
    enabled: bool
    mode: str
    movement_type: str
    identity_no_of_transport: str
    nationality_of_transport: str
    arrival_port: str
    place_of_loading: str
    place_of_unloading: str
    transport_charges: str
    carrier_eori: str
    carrier_name: str
    carrier_street_number: str
    carrier_city: str
    carrier_postcode: str
    carrier_country: str
    haulier_eori: str
    controlled_goods: str
    goods_domestic_status: str
    container_indicator: str
    country_of_origin: str
    package_type: str
    procedure_code: str
    additional_procedure_code: str
    valuation_method: str
    invoice_currency: str
    importer_eori: str
    importer_name: str
    importer_street_number: str
    importer_city: str
    importer_postcode: str
    importer_country: str
    consignor_eori: str
    exporter_eori: str
    supplier_name: str
    auto_validate: bool
    arrival_hours_ahead: int
    sdi_representation_type: str = "3"
    sdi_incoterm: str = "DDP"
    sdi_postponed_vat: str = "no"
    sdi_goods_domestic_status: str = "D"
    sdi_movement_type: str = "3"
    sdi_nature_of_transaction: str = "11"
    sdi_ni_additional_information_codes: str = "NIREM"

    @property
    def auto_create_enabled(self) -> bool:
        return self.enabled and self.mode == 'auto_create_if_clean'

    def build_arrival_datetime(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        dt = (now + timedelta(hours=self.arrival_hours_ahead)).replace(second=0, microsecond=0)
        return dt.strftime('%d/%m/%Y %H:%M:%S')


@dataclass
class ImapSettings:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    folder: str
    processed_folder: str
    search: str


@dataclass
class GraphMailSettings:
    enabled: bool
    tenant_id: str
    client_id: str
    client_secret: str
    mailbox: str
    folder: str
    processed_folder: str
    unread_only: bool
    max_messages: int
    allowed_sender_domains: tuple[str, ...] = ()


def _as_domain_tuple(value: str) -> tuple[str, ...]:
    domains = []
    for item in str(value or '').split(','):
        domain = item.strip().lower()
        if domain.startswith('@'):
            domain = domain[1:]
        if domain:
            domains.append(domain)
    return tuple(dict.fromkeys(domains))


def resolve_ingest_defaults(tenant_code: str | None = None) -> IngestDefaults:
    values = _load_category_values('INGEST_AUTO', tenant_code=tenant_code)
    sdi_values = _load_category_values('SDI_AUTO', tenant_code=tenant_code)
    company = _load_company_values(tenant_code=tenant_code)
    company_eori = str(company.get("eori_xi") or company.get("eori_gb") or "").strip()
    company_name = str(company.get("trading_name") or company.get("company_name") or "").strip()
    return IngestDefaults(
        enabled=_as_bool(_cfg(values, 'INGEST_AUTO', 'ENABLED', 'true'), True),
        mode=_cfg(values, 'INGEST_AUTO', 'MODE', 'review_required'),
        movement_type=_cfg(values, 'INGEST_AUTO', 'DEFAULT_MOVEMENT_TYPE', '1'),
        identity_no_of_transport=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IDENTITY_NO_OF_TRANSPORT', 'IMO1234567'),
        nationality_of_transport=_cfg(values, 'INGEST_AUTO', 'DEFAULT_NATIONALITY_OF_TRANSPORT', 'GB'),
        arrival_port=_cfg(values, 'INGEST_AUTO', 'DEFAULT_ARRIVAL_PORT', ''),
        place_of_loading=_cfg(values, 'INGEST_AUTO', 'DEFAULT_PLACE_OF_LOADING', ''),
        place_of_unloading=_cfg(values, 'INGEST_AUTO', 'DEFAULT_PLACE_OF_UNLOADING', ''),
        transport_charges=_cfg(values, 'INGEST_AUTO', 'DEFAULT_TRANSPORT_CHARGES', 'A'),
        carrier_eori=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_EORI', ''),
        carrier_name=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_NAME', ''),
        carrier_street_number=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_STREET_NUMBER', ''),
        carrier_city=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_CITY', ''),
        carrier_postcode=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_POSTCODE', ''),
        carrier_country=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CARRIER_COUNTRY', 'GB'),
        haulier_eori=_cfg(values, 'INGEST_AUTO', 'DEFAULT_HAULIER_EORI', ''),
        controlled_goods=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CONTROLLED_GOODS', 'no'),
        goods_domestic_status=_cfg(values, 'INGEST_AUTO', 'DEFAULT_GOODS_DOMESTIC_STATUS', ''),
        container_indicator=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CONTAINER_INDICATOR', '0'),
        country_of_origin=_cfg(values, 'INGEST_AUTO', 'DEFAULT_COUNTRY_OF_ORIGIN', 'GB'),
        package_type=_cfg(values, 'INGEST_AUTO', 'DEFAULT_PACKAGE_TYPE', 'PK'),
        procedure_code=_cfg(values, 'INGEST_AUTO', 'DEFAULT_PROCEDURE_CODE', '4000'),
        additional_procedure_code=_cfg(values, 'INGEST_AUTO', 'DEFAULT_ADDITIONAL_PROCEDURE_CODE', '000'),
        valuation_method=_cfg(values, 'INGEST_AUTO', 'DEFAULT_VALUATION_METHOD', '1'),
        invoice_currency=_cfg(values, 'INGEST_AUTO', 'DEFAULT_INVOICE_CURRENCY', 'GBP'),
        importer_eori=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_EORI', company_eori),
        importer_name=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_NAME', company_name),
        importer_street_number=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_STREET_NUMBER', company.get("address_line1", "")),
        importer_city=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_CITY', company.get("city", "")),
        importer_postcode=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_POSTCODE', company.get("postcode", "")),
        importer_country=_cfg(values, 'INGEST_AUTO', 'DEFAULT_IMPORTER_COUNTRY', company.get("country", "GB") or "GB"),
        consignor_eori=_cfg(values, 'INGEST_AUTO', 'DEFAULT_CONSIGNOR_EORI', ''),
        exporter_eori=_cfg(values, 'INGEST_AUTO', 'DEFAULT_EXPORTER_EORI', ''),
        supplier_name=_cfg(values, 'INGEST_AUTO', 'SUPPLIER_NAME', ''),
        auto_validate=_as_bool(_cfg(values, 'INGEST_AUTO', 'AUTO_VALIDATE', 'true'), True),
        arrival_hours_ahead=_as_int(_cfg(values, 'INGEST_AUTO', 'DEFAULT_ARRIVAL_HOURS_AHEAD', '4'), 4),
        sdi_representation_type=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_REPRESENTATION_TYPE', '3'),
        sdi_incoterm=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_INCOTERM', 'DDP'),
        sdi_postponed_vat=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_POSTPONED_VAT', 'no'),
        sdi_goods_domestic_status=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_GOODS_DOMESTIC_STATUS', 'D'),
        sdi_movement_type=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_MOVEMENT_TYPE', '3'),
        sdi_nature_of_transaction=_cfg(sdi_values, 'SDI_AUTO', 'DEFAULT_NATURE_OF_TRANSACTION', '11'),
        sdi_ni_additional_information_codes=_cfg(
            sdi_values,
            'SDI_AUTO',
            'DEFAULT_NI_ADDITIONAL_INFORMATION_CODES',
            'NIREM',
        ),
    )


def resolve_imap_settings(tenant_code: str | None = None) -> ImapSettings:
    values = _load_category_values('IMAP', tenant_code=tenant_code)
    return ImapSettings(
        enabled=_as_bool(_cfg(values, 'IMAP', 'ENABLED', 'false'), False),
        host=_cfg(values, 'IMAP', 'HOST', ''),
        port=_as_int(_cfg(values, 'IMAP', 'PORT', '993'), 993),
        username=_cfg(values, 'IMAP', 'USERNAME', ''),
        password=_cfg(values, 'IMAP', 'PASSWORD', ''),
        folder=_cfg(values, 'IMAP', 'FOLDER', 'INBOX'),
        processed_folder=_cfg(values, 'IMAP', 'PROCESSED_FOLDER', ''),
        search=_cfg(values, 'IMAP', 'SEARCH', 'UNSEEN'),
    )


def resolve_graph_mail_settings(tenant_code: str | None = None) -> GraphMailSettings:
    values = _load_category_values('GRAPH', tenant_code=tenant_code)
    mailbox = _cfg(values, 'GRAPH', 'MAILBOX', '')
    folder = _cfg(values, 'GRAPH', 'FOLDER', 'INBOX')
    if _use_syd_support_mailbox_bkd_folder_default(tenant_code, mailbox, folder):
        folder = 'BKD'
    default_allowed_domains = 'birkdalesales.com' if str(tenant_code or '').strip().upper() == 'BKD' else ''
    return GraphMailSettings(
        enabled=_as_bool(_cfg_env_override(values, 'GRAPH', 'ENABLED', 'false'), False),
        tenant_id=_cfg(values, 'GRAPH', 'TENANT_ID', ''),
        client_id=_cfg(values, 'GRAPH', 'CLIENT_ID', ''),
        client_secret=_cfg(values, 'GRAPH', 'CLIENT_SECRET', ''),
        mailbox=mailbox,
        folder=folder,
        processed_folder=_cfg(values, 'GRAPH', 'PROCESSED_FOLDER', ''),
        unread_only=_as_bool(_cfg(values, 'GRAPH', 'UNREAD_ONLY', 'true'), True),
        max_messages=_as_int(_cfg(values, 'GRAPH', 'MAX_MESSAGES', '50'), 50),
        allowed_sender_domains=_as_domain_tuple(
            _cfg_env_override(values, 'GRAPH', 'ALLOWED_SENDER_DOMAINS', default_allowed_domains)
        ),
    )


def _use_syd_support_mailbox_bkd_folder_default(tenant_code: str | None, mailbox: str, folder: str) -> bool:
    """Demo SYD uses the support mailbox folder named BKD; production tenants stay config-driven."""
    tenant = str(tenant_code or '').strip().upper()
    if tenant != 'SYD':
        return False
    if str(folder or '').strip().casefold() != 'inbox':
        return False
    return str(mailbox or '').strip().casefold() == 'support@synoviaintegration.com'
