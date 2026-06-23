"""
Simple Microsoft Graph downloader for customer inbound emails.

This file is intentionally written as a clear step-by-step script.

Current confirmed tenant:
    Birkdale / BKD

Current confirmed behavior:
    - Emails from @birkdalesales.com go to BKD.
    - If the email has file attachments, save the attachments.
    - Excel attachments are validated from the Graph attachment content.
    - A validation CSV is written with errors, warnings, and pending mapping items.
    - If the email has no file attachments, keep it visible for the future API/body stage.

No-attachment note:
    Emails without file attachments are skipped by this script today. The email
    body may be required later by the API/test stage to create ENS records and
    consignments, so that logic must stay outside this Graph-only downloader for now.

Important security note:
    This shared script does not store the real client secret in source code.
    Put GRAPH_TENANT_ID, GRAPH_CLIENT_ID, and GRAPH_CLIENT_SECRET in the
    runtime environment. Later, those values should come from CFG tables.
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


SCRIPT_FOLDER = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_FOLDER.parent


def load_dotenv_file() -> None:
    """
    Load .env values for simple manual execution.

    Supported key styles:
        GRAPH_TENANT_ID=...
        GRAPH.TENANT_ID=...
    """
    aliases = {
        "GRAPH.TENANT_ID": "GRAPH_TENANT_ID",
        "GRAPH.CLIENT_ID": "GRAPH_CLIENT_ID",
        "GRAPH.CLIENT_SECRET": "GRAPH_CLIENT_SECRET",
        "GRAPH.MAILBOX": "GRAPH_MAILBOX",
        "GRAPH.FOLDER": "GRAPH_FOLDER",
        "GRAPH.HISTORIC_START_DATE": "GRAPH_HISTORIC_START_DATE",
    }
    candidates = [
        Path.cwd() / ".env",
        SCRIPT_FOLDER / ".env",
        SCRIPT_FOLDER.parent / ".env",
    ]

    for env_path in candidates:
        if not env_path.exists():
            continue
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip().lstrip("\ufeff")
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
            alias = aliases.get(key)
            if alias and alias not in os.environ:
                os.environ[alias] = value
        return


load_dotenv_file()


# =============================================================================
# STEP 1 - GRAPH CONFIGURATION
# =============================================================================
#
# These are the Microsoft Graph connection settings.
#
# Keep the real secret outside the script because this file is on a shared
# QAS workspace. For a manual test, set these in PowerShell before running:
#
#   $env:GRAPH_TENANT_ID = "<tenant-id>"
#   $env:GRAPH_CLIENT_ID = "<client-id>"
#   $env:GRAPH_CLIENT_SECRET = "<client-secret>"
#   $env:GRAPH_MAILBOX = "nexus@synoviaflow.cloud"
#
# Future database version:
#   These values should be loaded from CFG.GraphSetting or similar.

GRAPH_TENANT_ID = os.getenv("GRAPH_TENANT_ID", "").strip()
GRAPH_CLIENT_ID = os.getenv("GRAPH_CLIENT_ID", "").strip()
GRAPH_CLIENT_SECRET = os.getenv("GRAPH_CLIENT_SECRET", "").strip()
GRAPH_MAILBOX = os.getenv("GRAPH_MAILBOX", "nexus@synoviaflow.cloud").strip()
GRAPH_FOLDER = os.getenv("GRAPH_FOLDER", "inbox").strip() or "inbox"
GRAPH_HISTORIC_START_DATE = os.getenv("GRAPH_HISTORIC_START_DATE", "2026-05-07").strip()

GRAPH_API_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_TOKEN_URL = (
    f"https://login.microsoftonline.com/"
    f"{urllib.parse.quote(GRAPH_TENANT_ID, safe='')}/oauth2/v2.0/token"
)


# =============================================================================
# STEP 2 - SHARED DRIVE CONFIGURATION
# =============================================================================

GRAPH_SCRIPT_FOLDER = SCRIPT_FOLDER
CUSTOMER_CONFIG_FOLDER = GRAPH_SCRIPT_FOLDER / "config" / "customers"

DEFAULT_INTEGRATION_LAYER_ROOT = REPO_ROOT / "Integration_Layer"
INTEGRATION_LAYER_ROOT = Path(
    os.getenv("FUSION_INTEGRATION_LAYER_ROOT", str(DEFAULT_INTEGRATION_LAYER_ROOT))
)

# Secondary technical log folder. The business objective is the tenant file
# destination under Integration_Layer; this log only helps during support checks.
RUN_LOG_FOLDER = GRAPH_SCRIPT_FOLDER / "run_logs"

# Tracks destination names selected during the current run. This lets the script
# save two same-name files received on the same date as "_2", while keeping
# reruns idempotent when the files already exist.
USED_DESTINATION_PATHS: set[Path] = set()


# =============================================================================
# STEP 3 - CUSTOMER YML CONFIGURATION
# =============================================================================
#
# Customer routing is stored as one .yml file per customer:
#
#   Graph/config/customers/BKD.yml
#   Graph/config/customers/CWH.yml
#
# The parser below is intentionally small. It supports key/value pairs and
# list values using "- item". This keeps the script dependency-free while the
# database CFG.Graph table is not ready.

FALLBACK_BIRKDALE_CONFIG = {
    "tenant_name": "Birkdale",
    "tenant_code": "BKD",
    "active": True,
    "sender_domains": ["birkdalesales.com"],
    "sender_addresses": [],
    "file_types": [".xlsx"],
    "save_mode": "attachments_only",
    "destination_folder": (
        INTEGRATION_LAYER_ROOT / "BKD" / "Inbound" / "Sales_Order_files"
    ),
}

# NO-ATTACHMENT BEHAVIOUR:
# Current BKD Graph processing is attachment-only. Emails without file attachments
# are skipped and counted for visibility. Body extraction belongs to the future
# API/test stage because the body can contain the ENS/consignment source data.


# =============================================================================
# STEP 4 - FUTURE TENANTS TODO
# =============================================================================
#
# For each future tenant, investigate and confirm:
#
#   1. Sender domain(s), for example @customer-domain.com.
#   2. Sender address(es), if one exact mailbox is required.
#   3. Whether the ENS/consignment source data arrives in the email body, in attachments, or both.
#   4. Attachment file types and whether any file should be ignored.
#   5. Whether no-attachment emails should be routed to the API/test body-processing stage.
#   6. Exact Integration Layer destination folder.
#   7. Whether the message can be marked as read after successful processing.
#
# When CFG tables are ready, this list should come from the database.

def parse_yml_value(value: str) -> Any:
    """Parse one simple YML scalar value used by customer config files."""
    value = value.strip()
    if value in {"[]", ""}:
        return []
    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        value = value[1:-1]

    lower_value = value.lower()
    if lower_value in {"true", "yes", "on"}:
        return True
    if lower_value in {"false", "no", "off"}:
        return False
    return value


def read_simple_yml(path: Path) -> dict[str, Any]:
    """
    Read a small, dependency-free YML file.

    Supported syntax:
        key: value
        key:
          - value 1
          - value 2
    """
    data: dict[str, Any] = {}
    current_list_key = ""

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        stripped = line.strip()
        if stripped.startswith("- "):
            if not current_list_key:
                raise ValueError(f"{path}:{line_number} list item without a key")
            data.setdefault(current_list_key, []).append(parse_yml_value(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"{path}:{line_number} expected 'key: value'")

        key, value = stripped.split(":", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip()
        if not key:
            raise ValueError(f"{path}:{line_number} empty key")

        if value:
            data[key] = parse_yml_value(value)
            current_list_key = ""
        else:
            data[key] = []
            current_list_key = key

    return data


def as_list(value: Any) -> list[str]:
    """Return config value as a clean list of strings."""
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def normalise_file_types(values: list[str]) -> list[str]:
    """Return file extensions in lowercase, always starting with a dot."""
    file_types = []
    for value in values:
        text = value.lower().strip()
        if not text:
            continue
        if not text.startswith("."):
            text = "." + text
        file_types.append(text)
    return file_types


def build_customer_config(path: Path, raw_config: dict[str, Any]) -> dict[str, Any] | None:
    """Validate one customer YML file and convert it to the script config shape."""
    active = bool(raw_config.get("active", True))
    if not active:
        return None

    tenant_code = str(raw_config.get("tenant_code") or path.stem).strip().upper()
    tenant_name = str(raw_config.get("tenant_name") or tenant_code).strip()
    destination_folder = str(raw_config.get("destination_folder") or "").strip()

    if not destination_folder:
        raise ValueError(f"{path}: destination_folder is required")

    destination_path = Path(destination_folder)
    if not destination_path.is_absolute():
        destination_path = REPO_ROOT / destination_path

    config = dict(raw_config)
    config.update(
        {
            "tenant_name": tenant_name,
            "tenant_code": tenant_code,
            "active": active,
            "sender_domains": [
                domain.lower().lstrip("@") for domain in as_list(raw_config.get("sender_domains"))
            ],
            "sender_addresses": [
                address.lower() for address in as_list(raw_config.get("sender_addresses"))
            ],
            "file_types": normalise_file_types(as_list(raw_config.get("file_types"))),
            "save_mode": str(raw_config.get("save_mode") or "attachments_only").strip().lower(),
            "historic_start_date": str(raw_config.get("historic_start_date") or "").strip(),
            "destination_folder": destination_path,
            "config_file": str(path),
        }
    )
    return config

def load_tenant_configs() -> list[dict[str, Any]]:
    """Load active customer configs from Graph/config/customers/*.yml."""
    paths = sorted(CUSTOMER_CONFIG_FOLDER.glob("*.yml"))
    configs = []

    for path in paths:
        config = build_customer_config(path, read_simple_yml(path))
        if config:
            configs.append(config)

    if configs:
        return configs

    # Fallback keeps the script usable if the config folder has not been copied
    # yet. Normal operation should use the customer YML files.
    return [FALLBACK_BIRKDALE_CONFIG]


TENANT_CONFIGS = load_tenant_configs()


# =============================================================================
# STEP 5 - EXCEL MAPPING AND API VALIDATION CONFIGURATION
# =============================================================================
#
# Files are saved locally for audit/support, but the preferred processing path is
# direct: read the Graph attachment bytes, parse the Excel in memory, map columns,
# and report API readiness issues immediately.
#
# Tenant config can extend aliases with keys such as:
#
#   map_transport_document_number:
#     - Customer TDN
#     - Document Number
#
# The code below keeps every source column accounted for: mapped to an API field,
# retained for audit, or reported as an unmapped source column.

EXCEL_SUFFIXES = {".xlsx"}

FIELD_RULES: dict[str, dict[str, Any]] = {
    "transport_document_number": {
        "label": "Transport document number",
        "required": True,
        "max_length": 35,
        "aliases": ["transport_document_number", "transport doc number", "tdn", "transport document", "document number", "document no"],
    },
    "goods_description": {
        "label": "Goods description",
        "required": True,
        "max_length": 255,
        "aliases": ["goods_description", "description", "item description", "product description", "commodity description", "goods desc"],
    },
    "controlled_goods": {
        "label": "Controlled goods",
        "required": True,
        "value_type": "yes_no",
        "aliases": ["controlled_goods", "controlled goods", "is controlled goods", "controlled"],
    },
    "consignor_eori": {
        "label": "Consignor EORI",
        "required": True,
        "max_length": 200,
        "value_type": "eori",
        "aliases": ["consignor_eori", "consignor eori", "shipper eori", "sender eori"],
    },
    "consignee_eori": {
        "label": "Consignee EORI",
        "required": True,
        "max_length": 200,
        "value_type": "eori",
        "aliases": ["consignee_eori", "consignee eori", "receiver eori"],
    },
    "importer_eori": {
        "label": "Importer EORI",
        "required": True,
        "max_length": 200,
        "value_type": "eori",
        "aliases": ["importer_eori", "importer eori", "importer eori number"],
    },
    "exporter_eori": {
        "label": "Exporter EORI",
        "required": True,
        "max_length": 200,
        "value_type": "eori",
        "aliases": ["exporter_eori", "exporter eori"],
    },
    "type_of_packages": {
        "label": "Type of packages",
        "required": True,
        "max_length": 40,
        "aliases": ["type_of_packages", "type of packages", "package type", "kind of packages", "package kind"],
    },
    "number_of_packages": {
        "label": "Number of packages",
        "required": True,
        "value_type": "integer",
        "min": 1,
        "max": 99999,
        "aliases": ["number_of_packages", "number of packages", "packages", "package count", "no of packages", "number packages"],
    },
    "package_marks": {
        "label": "Package marks",
        "required": True,
        "max_length": 140,
        "aliases": ["package_marks", "package marks", "marks", "shipping marks"],
    },
    "gross_mass_kg": {
        "label": "Gross mass kg",
        "required": True,
        "value_type": "decimal",
        "max_digits": 13,
        "max_decimals": 2,
        "aliases": ["gross_mass_kg", "gross mass kg", "gross mass", "gross weight", "gross weight kg", "gross kg"],
    },
    "commodity_code": {
        "label": "Commodity code",
        "required": False,
        "value_type": "commodity_code",
        "aliases": ["commodity_code", "commodity code", "hs code", "tariff code", "cn code", "goods code"],
    },
    "country_of_origin": {
        "label": "Country of origin",
        "required": False,
        "value_type": "country_code",
        "aliases": ["country_of_origin", "country of origin", "origin country", "coo", "origin"],
    },
    "item_invoice_amount": {
        "label": "Invoice amount",
        "required": False,
        "value_type": "decimal",
        "max_digits": 13,
        "max_decimals": 2,
        "aliases": ["item_invoice_amount", "invoice amount", "item value", "value", "customs value"],
    },
    "item_invoice_currency": {
        "label": "Invoice currency",
        "required": False,
        "value_type": "currency_code",
        "aliases": ["item_invoice_currency", "invoice currency", "currency", "value currency"],
    },
}

AUDIT_COLUMN_ALIASES = {
    "goods_item_number": ["goods_item_number", "goods item number", "item number", "item no", "line number", "line no"],
    "trader_reference": ["trader_reference", "trader reference", "customer reference", "manifest reference"],
    "destination_country": ["destination_country", "destination country", "country of destination"],
}


# =============================================================================
# STEP 6 - MICROSOFT GRAPH FUNCTIONS
# =============================================================================

def check_graph_configuration() -> None:
    """Stop early if the Graph credentials were not configured."""
    missing = []
    if not GRAPH_TENANT_ID:
        missing.append("GRAPH_TENANT_ID")
    if not GRAPH_CLIENT_ID:
        missing.append("GRAPH_CLIENT_ID")
    if not GRAPH_CLIENT_SECRET:
        missing.append("GRAPH_CLIENT_SECRET")

    if missing:
        raise RuntimeError(
            "Missing Graph configuration: "
            + ", ".join(missing)
            + ". Set these values in the runtime environment."
        )


def get_graph_token() -> str:
    """Get an app-only Microsoft Graph token using client credentials."""
    form_data = urllib.parse.urlencode(
        {
            "client_id": GRAPH_CLIENT_ID,
            "client_secret": GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        GRAPH_TOKEN_URL,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    response = open_json_request(request)
    return response["access_token"]


def graph_get(token: str, url: str) -> dict[str, Any]:
    """Run one GET request against Microsoft Graph."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    return open_json_request(request)


def open_json_request(request: urllib.request.Request) -> dict[str, Any]:
    """Open an HTTP request and return JSON, with readable Graph errors."""
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Graph HTTP {error.code}: {details}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Graph connection error: {error}") from error


def build_messages_url(args: argparse.Namespace) -> str:
    """Build the first Graph URL used to read mailbox messages."""
    mailbox = urllib.parse.quote(GRAPH_MAILBOX, safe="")
    folder = urllib.parse.quote(GRAPH_FOLDER, safe="")

    query = {
        "$select": ",".join(
            [
                "id",
                "subject",
                "from",
                "receivedDateTime",
                "hasAttachments",
                "internetMessageId",
            ]
        ),
        "$orderby": "receivedDateTime asc",
        "$top": str(args.page_size),
    }

    filters = []
    if args.received_from:
        filters.append(f"receivedDateTime ge {normalise_graph_date(args.received_from)}")
    if args.received_to:
        filters.append(
            f"receivedDateTime le {normalise_graph_date(args.received_to, end_of_day=True)}"
        )
    if filters:
        query["$filter"] = " and ".join(filters)

    return (
        f"{GRAPH_API_ROOT}/users/{mailbox}/mailFolders/{folder}/messages?"
        f"{urllib.parse.urlencode(query, safe=', :')}"
    )


def read_messages(token: str, args: argparse.Namespace) -> list[dict[str, Any]]:
    """Read mailbox messages, following Graph pagination."""
    url = build_messages_url(args)
    messages: list[dict[str, Any]] = []

    while url:
        data = graph_get(token, url)
        for message in data.get("value", []):
            messages.append(message)
            if args.max_messages and len(messages) >= args.max_messages:
                return messages
        url = data.get("@odata.nextLink")

    return messages


def read_attachments(token: str, message_id: str) -> list[dict[str, Any]]:
    """Read all attachments for one email message."""
    mailbox = urllib.parse.quote(GRAPH_MAILBOX, safe="")
    message = urllib.parse.quote(message_id, safe="")
    url = f"{GRAPH_API_ROOT}/users/{mailbox}/messages/{message}/attachments"
    attachments: list[dict[str, Any]] = []

    while url:
        data = graph_get(token, url)
        attachments.extend(data.get("value", []))
        url = data.get("@odata.nextLink")

    return attachments


def read_one_attachment(
    token: str,
    message_id: str,
    attachment_id: str,
) -> dict[str, Any]:
    """Read one attachment directly if Graph did not include file content."""
    mailbox = urllib.parse.quote(GRAPH_MAILBOX, safe="")
    message = urllib.parse.quote(message_id, safe="")
    attachment = urllib.parse.quote(attachment_id, safe="")
    url = f"{GRAPH_API_ROOT}/users/{mailbox}/messages/{message}/attachments/{attachment}"
    return graph_get(token, url)


# =============================================================================
# STEP 7 - TENANT MATCHING
# =============================================================================

def get_sender_email(message: dict[str, Any]) -> str:
    """Return the sender email address in lowercase."""
    return (
        message.get("from", {})
        .get("emailAddress", {})
        .get("address", "")
        .strip()
        .lower()
    )


def find_tenant_config(message: dict[str, Any]) -> dict[str, Any] | None:
    """Match one email to one tenant config using sender email/domain."""
    sender_email = get_sender_email(message)
    sender_domain = sender_email.split("@")[-1] if "@" in sender_email else ""

    for config in TENANT_CONFIGS:
        sender_domains = [domain.lower() for domain in config["sender_domains"]]
        sender_addresses = [address.lower() for address in config["sender_addresses"]]

        if sender_email in sender_addresses:
            return config
        if sender_domain in sender_domains:
            return config

    return None


# =============================================================================
# STEP 8 - FILE HELPERS
# =============================================================================

def file_attachments_only(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep real file attachments and ignore inline images/signatures."""
    files = []
    for attachment in attachments:
        is_file = attachment.get("@odata.type") == "#microsoft.graph.fileAttachment"
        is_inline = bool(attachment.get("isInline"))
        has_name = bool(attachment.get("name"))

        if is_file and not is_inline and has_name:
            files.append(attachment)

    return files


def attachment_allowed_for_config(attachment: dict[str, Any], config: dict[str, Any]) -> bool:
    """Return True when the attachment extension is allowed for this customer."""
    allowed_file_types = [value.lower() for value in config.get("file_types", [])]
    if not allowed_file_types:
        return True

    suffix = Path(str(attachment.get("name") or "")).suffix.lower()
    return suffix in allowed_file_types


def safe_filename(value: str, fallback: str) -> str:
    """Make a Windows-safe filename."""
    value = (value or fallback).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value)
    value = value.rstrip(". ")
    return value[:140] or fallback


def received_date_for_filename(message: dict[str, Any]) -> str:
    """Return received date as dd.mm.yyyy for filenames."""
    value = message["receivedDateTime"].replace("Z", "+00:00")
    received = dt.datetime.fromisoformat(value)
    received = received.astimezone(dt.timezone.utc)
    return received.strftime("%d.%m.%Y")


def save_bytes(path: Path, content: bytes, args: argparse.Namespace) -> str:
    """
    Save one file.

    Returns:
        SAVED, DRY_RUN, or SKIPPED_EXISTS
    """
    if path.exists() and not args.overwrite:
        return "SKIPPED_EXISTS"

    if args.dry_run:
        return "DRY_RUN"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return "SAVED"


def choose_run_unique_path(base_path: Path, args: argparse.Namespace) -> Path:
    """Choose base path, or _2/_3 when this run already used that name."""
    if args.overwrite:
        return base_path

    path = base_path
    counter = 2
    while path in USED_DESTINATION_PATHS:
        path = base_path.with_name(f"{base_path.stem}_{counter}{base_path.suffix}")
        counter += 1

    USED_DESTINATION_PATHS.add(path)
    return path


def save_attachment_file(
    message: dict[str, Any],
    config: dict[str, Any],
    attachment: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[str, Path, bytes]:
    """Save one attachment for audit and return the same bytes for validation."""
    original_name = safe_filename(attachment.get("name", ""), "attachment")
    original_path = Path(original_name)
    filename = f"{original_path.stem}_{received_date_for_filename(message)}{original_path.suffix}"
    path = choose_run_unique_path(config["destination_folder"] / filename, args)

    content = base64.b64decode(attachment["contentBytes"])
    result = save_bytes(path, content, args)
    return result, path, content


def normalise_graph_date(value: str, end_of_day: bool = False) -> str:
    """Accept YYYY-MM-DD or full ISO date and return a Graph UTC datetime."""
    value = value.strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        time_part = "23:59:59" if end_of_day else "00:00:00"
        return f"{value}T{time_part}Z"

    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    parsed = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z")


def utc_today() -> dt.date:
    """Return today's date in UTC because Graph receivedDateTime is UTC."""
    return dt.datetime.now(dt.timezone.utc).date()


def date_only(value: str) -> str:
    """Return YYYY-MM-DD from a YYYY-MM-DD or ISO datetime value."""
    return normalise_graph_date(value)[:10]


def historic_start_date_from_configs() -> str:
    """Return the earliest configured historic start date across active customers."""
    candidates = []
    for config in TENANT_CONFIGS:
        value = str(config.get("historic_start_date") or "").strip()
        if value:
            candidates.append(date_only(value))

    if candidates:
        return min(candidates)
    return date_only(GRAPH_HISTORIC_START_DATE)


def apply_run_mode_dates(args: argparse.Namespace) -> None:
    """
    Resolve the date window before the mailbox query is built.

    Modes:
        daily    - default; reads only today's Graph emails.
        historic - reads from customer historic_start_date up to yesterday.
        custom   - uses only the dates passed by the operator.
    """
    today = utc_today()

    if args.run_mode == "daily":
        args.received_from = args.received_from or today.isoformat()
        args.received_to = args.received_to or today.isoformat()
        return

    if args.run_mode == "historic":
        yesterday = today - dt.timedelta(days=1)
        args.received_from = args.received_from or historic_start_date_from_configs()
        args.received_to = args.received_to or yesterday.isoformat()


# =============================================================================
# STEP 9 - EXCEL PARSING AND API READINESS VALIDATION
# =============================================================================

def normalize_column_name(value: str) -> str:
    """Return a stable comparison key for Excel headers and aliases."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def config_aliases_for_field(config: dict[str, Any], field_name: str) -> list[str]:
    """Read optional tenant aliases from map_<field_name> config keys."""
    return [normalize_column_name(value) for value in as_list(config.get(f"map_{field_name}"))]


def build_alias_lookup(config: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Build normalized alias -> (field kind, field name)."""
    lookup: dict[str, tuple[str, str]] = {}

    for field_name, rule in FIELD_RULES.items():
        aliases = [*rule.get("aliases", []), *config_aliases_for_field(config, field_name)]
        for alias in aliases:
            normalized = normalize_column_name(alias)
            if normalized:
                lookup[normalized] = ("api", field_name)

    for field_name, aliases in AUDIT_COLUMN_ALIASES.items():
        tenant_aliases = config_aliases_for_field(config, field_name)
        for alias in [*aliases, *tenant_aliases]:
            normalized = normalize_column_name(alias)
            if normalized and normalized not in lookup:
                lookup[normalized] = ("audit", field_name)

    for alias in as_list(config.get("audit_columns")):
        normalized = normalize_column_name(alias)
        if normalized and normalized not in lookup:
            lookup[normalized] = ("audit", "tenant_audit")

    return lookup


def configured_field_source(config: dict[str, Any], field_name: str) -> str:
    """Return non-Excel source configured for an API field, if present."""
    for key in (f"source_{field_name}", f"default_{field_name}", f"derive_{field_name}"):
        value = str(config.get(key, "")).strip()
        if value:
            return value
    return ""


def read_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    """Read XLSX shared strings with only the standard library."""
    try:
        xml = workbook.read("xl/sharedStrings.xml")
    except KeyError:
        return []

    root = ET.fromstring(xml)
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall("s:si", ns):
        values.append("".join(text.text or "" for text in item.findall(".//s:t", ns)))
    return values


def column_index_from_reference(cell_reference: str) -> int:
    """Return zero-based column index from an Excel cell reference like B12."""
    letters = re.sub(r"[^A-Z]", "", cell_reference.upper())
    index = 0
    for letter in letters:
        index = index * 26 + (ord(letter) - ord("A") + 1)
    return max(index - 1, 0)


def worksheet_path(workbook: zipfile.ZipFile) -> str:
    """Return the first worksheet path from an XLSX file."""
    ns = {
        "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    book_root = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    first_sheet = book_root.find("s:sheets/s:sheet", ns)
    if first_sheet is None:
        raise ValueError("Workbook has no worksheets")

    relationship_id = first_sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    for relationship in rels_root:
        if relationship.get("Id") == relationship_id:
            target = relationship.get("Target", "")
            return "xl/" + target.lstrip("/")

    raise ValueError("Workbook first worksheet relationship was not found")


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    """Return a readable value from one XLSX cell."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.get("t", "")

    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//s:t", ns)).strip()

    value = cell.find("s:v", ns)
    if value is None or value.text is None:
        return ""

    raw = value.text.strip()
    if cell_type == "s" and raw.isdigit():
        index = int(raw)
        if 0 <= index < len(shared_strings):
            return shared_strings[index].strip()
    return raw


def header_candidate_score(row: list[str], alias_lookup: dict[str, tuple[str, str]]) -> int:
    """Score a row as a possible Excel table header."""
    business_words = {
        "address",
        "amount",
        "commodity",
        "consignee",
        "consignor",
        "country",
        "currency",
        "customer",
        "description",
        "document",
        "email",
        "eori",
        "exporter",
        "gross",
        "importer",
        "invoice",
        "item",
        "line",
        "marks",
        "measure",
        "number",
        "origin",
        "package",
        "packages",
        "postcode",
        "price",
        "quantity",
        "qty",
        "ship",
        "tariff",
        "unit",
        "weight",
    }
    technical_words = {
        "autohide",
        "autotable",
        "fields",
        "filters",
        "formula",
        "formulas",
        "headers",
        "hide",
        "linkfield",
        "links",
        "lookup",
        "option",
        "tables",
        "values",
    }

    normalized_values = [normalize_column_name(value) for value in row if str(value or "").strip()]
    if len(normalized_values) < 2:
        return -100

    score = min(len(normalized_values), 25)
    for normalized in normalized_values:
        words = set(normalized.split())
        if normalized in alias_lookup:
            score += 10
        score += len(words & business_words)
        if any(word in normalized for word in technical_words):
            score -= 8

    return score


def select_header_index(raw_rows: list[list[str]]) -> int:
    """Choose the most likely business header row from an Excel worksheet."""
    alias_lookup = build_alias_lookup({})
    best_index = 0
    best_score = -999

    for index, row in enumerate(raw_rows[:60]):
        score = header_candidate_score(row, alias_lookup)
        if score > best_score:
            best_index = index
            best_score = score

    if best_score > 2:
        return best_index

    for index, row in enumerate(raw_rows[:10]):
        if sum(1 for value in row if value.strip()) >= 2:
            return index
    return 0


def unique_headers(raw_header_row: list[str]) -> list[str]:
    """Return readable unique headers while keeping blank columns obvious."""
    headers: list[str] = []
    seen: dict[str, int] = {}

    for index, value in enumerate(raw_header_row):
        header = value.strip() or f"Column {index + 1}"
        count = seen.get(header, 0) + 1
        seen[header] = count
        if count > 1:
            header = f"{header} ({count})"
        headers.append(header)

    return headers


def is_summary_excel_row(row_data: dict[str, str], header_count: int) -> bool:
    """Skip worksheet total rows that are not customer goods lines."""
    values = [normalize_column_name(value) for value in row_data.values() if str(value or "").strip()]
    if not values:
        return True
    has_total_marker = any(value in {"total", "grand total"} for value in values)
    return has_total_marker and len(values) < max(6, header_count // 2)

def read_xlsx_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Read first worksheet into headers and row dictionaries."""
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        shared_strings = read_shared_strings(workbook)
        sheet_xml = workbook.read(worksheet_path(workbook))

    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(sheet_xml)
    raw_rows: list[list[str]] = []

    for row in root.findall(".//s:sheetData/s:row", ns):
        values: list[str] = []
        for cell in row.findall("s:c", ns):
            column_index = column_index_from_reference(cell.get("r", "A1"))
            while len(values) <= column_index:
                values.append("")
            values[column_index] = cell_text(cell, shared_strings)
        if any(value.strip() for value in values):
            raw_rows.append(values)

    if not raw_rows:
        return [], []

    header_index = select_header_index(raw_rows)
    headers = unique_headers(raw_rows[header_index])
    rows: list[dict[str, str]] = []

    for raw_row in raw_rows[header_index + 1:]:
        row_data = {}
        for index, header in enumerate(headers):
            row_data[header] = raw_row[index].strip() if index < len(raw_row) else ""
        if any(value.strip() for value in row_data.values()) and not is_summary_excel_row(row_data, len(headers)):
            rows.append(row_data)

    return headers, rows

def add_validation_issue(
    validation_rows: list[dict[str, str]],
    message: dict[str, Any],
    config: dict[str, Any],
    attachment_name: str,
    saved_path: Path | str,
    row_number: int,
    source_column: str,
    technical_field: str,
    severity: str,
    rule: str,
    details: str,
) -> None:
    """Append one validation issue to the run validation report."""
    validation_rows.append(
        {
            "receivedDateTime": message.get("receivedDateTime", ""),
            "tenantCode": config.get("tenant_code", ""),
            "tenantName": config.get("tenant_name", ""),
            "sender": get_sender_email(message),
            "subject": message.get("subject", ""),
            "attachmentName": attachment_name,
            "savedPath": str(saved_path),
            "rowNumber": str(row_number),
            "sourceColumn": source_column,
            "technicalField": technical_field,
            "severity": severity,
            "rule": rule,
            "details": details,
        }
    )


def mapped_columns(headers: list[str], config: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Return mapping details keyed by source header."""
    lookup = build_alias_lookup(config)
    mapping = {}
    for header in headers:
        normalized = normalize_column_name(header)
        if normalized.startswith("column ") or normalized in {"autotable", "auto table"}:
            mapping[header] = {"kind": "ignored", "field_name": ""}
            continue
        kind, field_name = lookup.get(normalized, ("unmapped", ""))
        mapping[header] = {"kind": kind, "field_name": field_name}
    return mapping


def first_source_column_for_field(mapping: dict[str, dict[str, str]], field_name: str) -> str:
    """Return the source column mapped to a technical field, if any."""
    for source_column, details in mapping.items():
        if details["field_name"] == field_name:
            return source_column
    return ""


def validate_required_mappings(
    headers: list[str],
    mapping: dict[str, dict[str, str]],
    validation_rows: list[dict[str, str]],
    message: dict[str, Any],
    config: dict[str, Any],
    attachment_name: str,
    saved_path: Path | str,
) -> None:
    """Report every source column and missing mandatory API mappings."""
    for source_column, details in mapping.items():
        if details["kind"] == "unmapped":
            add_validation_issue(
                validation_rows,
                message,
                config,
                attachment_name,
                saved_path,
                0,
                source_column,
                "",
                "PENDING",
                "Unmapped source column",
                "Column is present in the Excel but not currently mapped to API, derived, audit, or ignored data.",
            )
        elif details["kind"] == "audit":
            add_validation_issue(
                validation_rows,
                message,
                config,
                attachment_name,
                saved_path,
                0,
                source_column,
                details["field_name"],
                "INFO",
                "Audit column accounted for",
                "Column is retained for traceability/support and is not sent directly to the API.",
            )

    for field_name, rule in FIELD_RULES.items():
        if not rule.get("required"):
            continue
        if not first_source_column_for_field(mapping, field_name):
            configured_source = configured_field_source(config, field_name)
            if configured_source:
                add_validation_issue(
                    validation_rows,
                    message,
                    config,
                    attachment_name,
                    saved_path,
                    0,
                    configured_source,
                    field_name,
                    "INFO",
                    "Mandatory API field source accounted for",
                    "Field is not expected as an Excel column; it must be resolved from this configured source before API submission.",
                )
                continue
            add_validation_issue(
                validation_rows,
                message,
                config,
                attachment_name,
                saved_path,
                0,
                "",
                field_name,
                "ERROR",
                "Missing API mandatory mapping",
                "No Excel column is mapped to this mandatory API field. Provide a column or approved tenant/master-data source.",
            )


def is_blank(value: str) -> bool:
    """Return True for empty Excel values."""
    return str(value or "").strip() == ""


def decimal_parts(value: str) -> tuple[str, str]:
    """Return integer and decimal parts for simple numeric validation."""
    text = value.strip().replace(" ", "")
    if text.startswith("-"):
        text = text[1:]
    if "." in text:
        left, right = text.split(".", 1)
        return left, right
    return text, ""


def normalize_excel_decimal_noise(value: str, max_decimals: int) -> str:
    """Collapse harmless Excel floating-point noise before decimal validation."""
    if "." not in value:
        return value
    try:
        number = float(value)
    except ValueError:
        return value

    rounded = f"{number:.{max_decimals}f}"
    if abs(number - float(rounded)) < 0.000000001:
        return rounded.rstrip("0").rstrip(".") or "0"
    return value

def validate_field_value(field_name: str, value: str) -> list[tuple[str, str, str]]:
    """Return validation issues as severity/rule/details tuples."""
    rule = FIELD_RULES[field_name]
    issues: list[tuple[str, str, str]] = []
    text = str(value or "").strip()

    if rule.get("required") and is_blank(text):
        return [("ERROR", "Missing API mandatory value", "Value is required by the TSS/API contract.")]
    if is_blank(text):
        return []

    max_length = rule.get("max_length")
    if max_length and len(text) > int(max_length):
        issues.append(("ERROR", "API maximum length", f"Value has {len(text)} characters; maximum is {max_length}."))

    value_type = rule.get("value_type")
    if value_type == "yes_no" and text.lower() not in {"yes", "no", "y", "n", "true", "false", "1", "0"}:
        issues.append(("ERROR", "Yes/no value", "Expected yes/no, true/false, or 1/0."))

    if value_type == "integer":
        if not re.fullmatch(r"\d+", text):
            issues.append(("ERROR", "Integer value", "Expected a whole number."))
        else:
            number = int(text)
            if number < int(rule.get("min", 0)) or number > int(rule.get("max", number)):
                issues.append(("ERROR", "Integer range", f"Expected value between {rule.get('min')} and {rule.get('max')}."))

    if value_type == "decimal":
        if "," in text:
            issues.append(("ERROR", "Decimal format", "Commas are not allowed in API numeric values."))
        normalized = text.replace(" ", "")
        max_decimals = int(rule.get("max_decimals", 2))
        normalized = normalize_excel_decimal_noise(normalized, max_decimals)
        if not re.fullmatch(r"-?\d+(\.\d+)?", normalized):
            issues.append(("ERROR", "Decimal value", "Expected a valid number."))
        else:
            left, right = decimal_parts(normalized)
            total_digits = len(left.lstrip("0")) + len(right)
            if total_digits > int(rule.get("max_digits", total_digits)):
                issues.append(("ERROR", "Decimal max digits", f"Maximum total digits is {rule.get('max_digits')}."))
            if len(right) > max_decimals:
                issues.append(("ERROR", "Decimal precision", f"Maximum decimals is {rule.get('max_decimals')}."))

    if value_type == "eori" and field_name == "consignor_eori" and text.upper().startswith("GB"):
        issues.append(("ERROR", "Consignor EORI", "TSS guidance says GB EORI is not accepted for consignor_eori."))

    if value_type == "commodity_code":
        if not re.fullmatch(r"\d+", text):
            issues.append(("ERROR", "Commodity code format", "Commodity code must contain digits only."))
        elif len(text) < 6 or len(text) == 7 or len(text) > 10:
            issues.append(("ERROR", "Commodity code length", "Expected 8-10 digits, or 6 digits only when APC is 1SG."))
        elif len(text) == 6:
            issues.append(("WARNING", "Conditional commodity code", "6 digits are only valid when APC is 1SG; confirm before API submission."))

    if value_type == "country_code" and not re.fullmatch(r"[A-Za-z]{2}", text):
        issues.append(("ERROR", "Country code", "Expected a 2-letter country code."))

    if value_type == "currency_code" and not re.fullmatch(r"[A-Za-z]{3}", text):
        issues.append(("WARNING", "Currency code", "Expected a 3-letter currency code when supplied."))

    return issues


def validate_xlsx_attachment(
    message: dict[str, Any],
    config: dict[str, Any],
    attachment_name: str,
    content: bytes,
    saved_path: Path | str,
    validation_rows: list[dict[str, str]],
    stats: dict[str, Any],
) -> None:
    """Parse and validate one Excel attachment directly from Graph bytes."""
    suffix = Path(attachment_name).suffix.lower()
    if suffix not in EXCEL_SUFFIXES:
        return

    stats["validated_excels"] += 1

    try:
        headers, data_rows = read_xlsx_rows(content)
    except Exception as error:
        stats["validation_errors"] += 1
        add_validation_issue(
            validation_rows,
            message,
            config,
            attachment_name,
            saved_path,
            0,
            "",
            "",
            "ERROR",
            "Unreadable Excel",
            str(error),
        )
        return

    mapping = mapped_columns(headers, config)
    issue_start = len(validation_rows)
    validate_required_mappings(headers, mapping, validation_rows, message, config, attachment_name, saved_path)

    for row_index, row in enumerate(data_rows, 2):
        for source_column, details in mapping.items():
            if details["kind"] != "api":
                continue
            field_name = details["field_name"]
            for severity, rule, details_text in validate_field_value(field_name, row.get(source_column, "")):
                if severity == "ERROR":
                    stats["validation_errors"] += 1
                elif severity == "WARNING":
                    stats["validation_warnings"] += 1
                else:
                    stats["validation_pending"] += 1
                add_validation_issue(
                    validation_rows,
                    message,
                    config,
                    attachment_name,
                    saved_path,
                    row_index,
                    source_column,
                    field_name,
                    severity,
                    rule,
                    details_text,
                )

    # Missing mandatory mappings are added after row counters so the summary also
    # reflects file-level errors.
    for issue in validation_rows[issue_start:]:
        if issue["rule"] == "Missing API mandatory mapping":
            stats["validation_errors"] += 1
        if issue["severity"] == "PENDING":
            stats["validation_pending"] += 1

# =============================================================================
# STEP 10 - QUALITY REPORT HELPERS
# =============================================================================

def new_stats() -> dict[str, Any]:
    """Create simple counters for console output and support checks."""
    return {
        "scanned": 0,
        "matched": 0,
        "unmatched": 0,
        "saved_attachments": 0,
        "skipped_existing": 0,
        "no_file_attachments": 0,
        "failed": 0,
        "validated_excels": 0,
        "validation_errors": 0,
        "validation_warnings": 0,
        "validation_pending": 0,
        "file_types": {},
    }


def count_file_type(stats: dict[str, Any], suffix: str) -> None:
    """Count saved file types for historic review."""
    suffix = suffix.lower() or ".no_extension"
    file_types = stats["file_types"]
    file_types[suffix] = file_types.get(suffix, 0) + 1


def report_row(
    message: dict[str, Any],
    config: dict[str, Any] | None,
    action: str,
    saved_path: Path | str = "",
    file_type: str = "",
    note: str = "",
) -> dict[str, str]:
    """Build one technical log row."""
    return {
        "receivedDateTime": message.get("receivedDateTime", ""),
        "tenantCode": config["tenant_code"] if config else "",
        "tenantName": config["tenant_name"] if config else "",
        "sender": get_sender_email(message),
        "subject": message.get("subject", ""),
        "action": action,
        "fileType": file_type,
        "savedPath": str(saved_path),
        "note": note,
        "messageId": message.get("internetMessageId") or message.get("id", ""),
    }


def write_run_log(
    rows: list[dict[str, str]],
    stats: dict[str, Any],
    started_at: dt.datetime,
    args: argparse.Namespace,
) -> Path:
    """Write a small technical run log; this is not the business objective."""
    RUN_LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    run_type = "dry_run" if args.dry_run else "run"
    path = RUN_LOG_FOLDER / f"graph_download_run_log_{started_at:%Y%m%d_%H%M%S}_{run_type}.csv"

    columns = [
        "receivedDateTime",
        "tenantCode",
        "tenantName",
        "sender",
        "subject",
        "action",
        "fileType",
        "savedPath",
        "note",
        "messageId",
    ]

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        writer.writerow({})
        writer.writerow({"action": "SUMMARY", "note": f"Scanned: {stats['scanned']}"})
        writer.writerow({"action": "SUMMARY", "note": f"Matched: {stats['matched']}"})
        writer.writerow({"action": "SUMMARY", "note": f"Unmatched: {stats['unmatched']}"})
        writer.writerow({"action": "SUMMARY", "note": f"Saved attachments: {stats['saved_attachments']}"})
        writer.writerow({"action": "SUMMARY", "note": f"Skipped existing: {stats['skipped_existing']}"})
        writer.writerow({
            "action": "SUMMARY",
            "note": (
                "No file attachments: "
                f"{stats['no_file_attachments']}"
            ),
        })
        writer.writerow({"action": "SUMMARY", "note": f"Failed: {stats['failed']}"})

        for suffix, count in sorted(stats["file_types"].items()):
            writer.writerow({"action": "FILE_TYPE", "fileType": suffix, "note": str(count)})

    return path



def write_validation_report(
    rows: list[dict[str, str]],
    started_at: dt.datetime,
    args: argparse.Namespace,
) -> Path | None:
    """Write Excel mapping/API validation output for support and QA."""
    if not rows:
        return None

    RUN_LOG_FOLDER.mkdir(parents=True, exist_ok=True)
    run_type = "dry_run" if args.dry_run else "run"
    path = RUN_LOG_FOLDER / f"graph_validation_report_{started_at:%Y%m%d_%H%M%S}_{run_type}.csv"

    columns = [
        "receivedDateTime",
        "tenantCode",
        "tenantName",
        "sender",
        "subject",
        "attachmentName",
        "savedPath",
        "rowNumber",
        "sourceColumn",
        "technicalField",
        "severity",
        "rule",
        "details",
    ]

    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    return path

# =============================================================================
# STEP 11 - PROCESS ONE EMAIL
# =============================================================================

def process_one_message(
    token: str,
    message: dict[str, Any],
    stats: dict[str, Any],
    rows: list[dict[str, str]],
    validation_rows: list[dict[str, str]],
    args: argparse.Namespace,
) -> None:
    """Process one email in the simplest possible order."""
    stats["scanned"] += 1
    config = find_tenant_config(message)

    if not config:
        stats["unmatched"] += 1
        rows.append(report_row(message, None, "SKIPPED_UNMATCHED_SENDER"))
        return

    stats["matched"] += 1

    try:
        # Step A: If there are attachments, save attachments and do not save body.
        attachments = []
        if message.get("hasAttachments"):
            attachments = read_attachments(token, message["id"])

        all_file_attachments = file_attachments_only(attachments)
        file_attachments = [
            attachment
            for attachment in all_file_attachments
            if attachment_allowed_for_config(attachment, config)
        ]

        if all_file_attachments and not file_attachments:
            allowed = ", ".join(config.get("file_types", [])) or "any"
            rows.append(
                report_row(
                    message,
                    config,
                    "SKIPPED_UNSUPPORTED_FILE_TYPE",
                    note=f"No attachment matched allowed file types: {allowed}",
                )
            )
            return

        if file_attachments:
            for attachment in file_attachments:
                # Some Graph list responses omit contentBytes. If so, read it directly.
                if not attachment.get("contentBytes") and attachment.get("id"):
                    attachment = read_one_attachment(token, message["id"], attachment["id"])

                result, path, content = save_attachment_file(message, config, attachment, args)
                suffix = path.suffix.lower() or ".no_extension"
                count_file_type(stats, suffix)

                if result in {"SAVED", "DRY_RUN"}:
                    stats["saved_attachments"] += 1
                elif result == "SKIPPED_EXISTS":
                    stats["skipped_existing"] += 1

                validate_xlsx_attachment(
                    message,
                    config,
                    attachment.get("name", ""),
                    content,
                    path,
                    validation_rows,
                    stats,
                )

                rows.append(
                    report_row(
                        message,
                        config,
                        f"{result}_ATTACHMENT",
                        saved_path=path,
                        file_type=suffix,
                        note=attachment.get("name", ""),
                    )
                )
            return

        # Step B: No file attachments.
        # Current Graph scope is attachment-only, so no body is saved here.
        # Future ENS/consignment creation from the body must run through the test API stage.
        stats["no_file_attachments"] += 1
        rows.append(
            report_row(
                message,
                config,
                "SKIPPED_NO_FILE_ATTACHMENTS",
                note="Attachment-only processing. No file attachments found.",
            )
        )

    except Exception as error:
        stats["failed"] += 1
        rows.append(report_row(message, config, "FAILED_MESSAGE", note=str(error)))
        print(f"ERROR processing message {message.get('id')}: {error}")


# =============================================================================
# STEP 12 - MAIN SCRIPT FLOW
# =============================================================================

def run(args: argparse.Namespace) -> int:
    r"""
    Main execution flow.

    Main objective:
        Save inbound files into the correct tenant folder under:
        \\PL-AZ-SDF-PLINT\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Integration_Layer

    Current confirmed example:
        BKD files are saved into:
        \\PL-AZ-SDF-PLINT\Fusion_Production\Scratch\Fusion_Flow_V3_QAS\Integration_Layer\BKD\Inbound\Sales_Order_files

    This is the order the operator should understand:
        1. Validate Graph configuration.
        2. Get Graph token.
        3. Read emails from the mailbox.
        4. For each email, find the tenant by sender.
        5. Save file attachments into that tenant's Integration Layer folder.
        6. Print a summary and keep a small technical run log for support.
    """
    started_at = dt.datetime.now(dt.timezone.utc)
    stats = new_stats()
    rows: list[dict[str, str]] = []
    validation_rows: list[dict[str, str]] = []

    print("STEP 1 - Checking Graph configuration")
    check_graph_configuration()

    print("STEP 2 - Getting Microsoft Graph token")
    token = get_graph_token()

    print(f"STEP 3 - Reading mailbox messages ({args.run_mode}: {args.received_from or 'no lower date'} to {args.received_to or 'no upper date'})")
    messages = read_messages(token, args)
    print(f"Found {len(messages)} message(s) to review")

    print("STEP 4 - Processing messages by tenant rules")
    for message in messages:
        process_one_message(token, message, stats, rows, validation_rows, args)

    print("STEP 5 - Finalising support summary")
    run_log_path = write_run_log(rows, stats, started_at, args)
    validation_report_path = write_validation_report(validation_rows, started_at, args)

    print_summary(stats, run_log_path, validation_report_path)
    return 1 if stats["failed"] else 0


def print_summary(
    stats: dict[str, Any],
    run_log_path: Path,
    validation_report_path: Path | None,
) -> None:
    """Print the final result in the terminal."""
    print("")
    print("Graph download finished")
    print(f"  Scanned messages: {stats['scanned']}")
    print(f"  Matched messages: {stats['matched']}")
    print(f"  Unmatched messages: {stats['unmatched']}")
    print(f"  Saved attachments: {stats['saved_attachments']}")
    print(f"  Skipped existing files: {stats['skipped_existing']}")
    print(
        "  No file attachments: "
        f"{stats['no_file_attachments']}"
    )
    print(f"  Failed messages: {stats['failed']}")
    print(f"  Validated Excel files: {stats['validated_excels']}")
    print(f"  Validation errors: {stats['validation_errors']}")
    print(f"  Validation warnings: {stats['validation_warnings']}")
    print(f"  Validation pending items: {stats['validation_pending']}")
    print(f"  Technical run log: {run_log_path}")
    if validation_report_path:
        print(f"  Validation report: {validation_report_path}")

    if stats["file_types"]:
        print("  File types:")
        for suffix, count in sorted(stats["file_types"].items()):
            print(f"    {suffix}: {count}")


# =============================================================================
# STEP 13 - COMMAND LINE OPTIONS
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Define optional filters for daily and historic downloads."""
    parser = argparse.ArgumentParser(
        description="Simple Microsoft Graph downloader by tenant sender domain."
    )
    parser.add_argument(
        "--run-mode",
        choices=["daily", "historic", "custom"],
        default="daily",
        help=(
            "daily reads today's emails only, historic reads from customer "
            "historic_start_date to yesterday, custom uses the explicit date filters."
        ),
    )
    parser.add_argument(
        "--received-from",
        help="Optional lower received date. Example: 2026-01-01",
    )
    parser.add_argument(
        "--received-to",
        help="Optional upper received date. Example: 2026-06-17",
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=0,
        help="Maximum messages to read. 0 means no script limit.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Graph page size. Use 1 to 100.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without writing customer files.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite destination files if they already exist.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    apply_run_mode_dates(args)

    if args.max_messages < 0:
        parser.error("--max-messages cannot be negative")
    if args.page_size < 1 or args.page_size > 100:
        parser.error("--page-size must be between 1 and 100")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
