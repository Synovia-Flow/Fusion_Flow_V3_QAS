"""
Simple Microsoft Graph downloader for customer inbound emails.

This file is intentionally written as a clear step-by-step script.

Current confirmed tenant:
    Birkdale / BKD

Current confirmed behavior:
    - Emails from @birkdalesales.com go to BKD.
    - If the email has file attachments, save the attachments.
    - If the email has no file attachments, report it as pending confirmation.

Body processing note:
    Body extraction is intentionally disabled for now. Ask Aidan whether body
    content should be saved later, and confirm the exact markers/rules first.

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
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_FOLDER = Path(__file__).resolve().parent


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
# production drive. For a manual test, set these in PowerShell before running:
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

INTEGRATION_LAYER_ROOT = Path(
    r"\\PL-AZ-SDF-PLINT\Fusion_Production"
    r"\Synovia_Flow_Production\Integration_Layer"
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

# BODY TODO TO ASK AIDAN:
# Earlier notes mentioned saving body text from "TSS FOR" or "DETAILS FOR"
# until "customsadmin@primelineexpress.co.uk".
# For the current historic task, we are downloading files only. Re-enable body
# extraction only after Aidan confirms that rule is still required.


# =============================================================================
# STEP 4 - FUTURE TENANTS TODO
# =============================================================================
#
# For each future tenant, investigate and confirm:
#
#   1. Sender domain(s), for example @customer-domain.com.
#   2. Sender address(es), if one exact mailbox is required.
#   3. Whether the order data arrives in the email body, in attachments, or both.
#   4. Attachment file types and whether any file should be ignored.
#   5. Body start marker and body end marker, if body extraction is required.
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

    return {
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
        "destination_folder": Path(destination_folder),
        "config_file": str(path),
    }


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
# STEP 5 - MICROSOFT GRAPH FUNCTIONS
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
# STEP 6 - TENANT MATCHING
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
# STEP 7 - FILE HELPERS
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
) -> tuple[str, Path]:
    """Save one attachment using original name plus received date."""
    original_name = safe_filename(attachment.get("name", ""), "attachment")
    original_path = Path(original_name)
    filename = f"{original_path.stem}_{received_date_for_filename(message)}{original_path.suffix}"
    path = choose_run_unique_path(config["destination_folder"] / filename, args)

    content = base64.b64decode(attachment["contentBytes"])
    result = save_bytes(path, content, args)
    return result, path


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


# =============================================================================
# STEP 8 - QUALITY REPORT HELPERS
# =============================================================================

def new_stats() -> dict[str, Any]:
    """Create simple counters for console output and support checks."""
    return {
        "scanned": 0,
        "matched": 0,
        "unmatched": 0,
        "saved_attachments": 0,
        "skipped_existing": 0,
        "no_file_attachments_pending_aidan": 0,
        "failed": 0,
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
                "No file attachments / body pending Aidan: "
                f"{stats['no_file_attachments_pending_aidan']}"
            ),
        })
        writer.writerow({"action": "SUMMARY", "note": f"Failed: {stats['failed']}"})

        for suffix, count in sorted(stats["file_types"].items()):
            writer.writerow({"action": "FILE_TYPE", "fileType": suffix, "note": str(count)})

    return path


# =============================================================================
# STEP 9 - PROCESS ONE EMAIL
# =============================================================================

def process_one_message(
    token: str,
    message: dict[str, Any],
    stats: dict[str, Any],
    rows: list[dict[str, str]],
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

                result, path = save_attachment_file(message, config, attachment, args)
                suffix = path.suffix.lower() or ".no_extension"
                count_file_type(stats, suffix)

                if result in {"SAVED", "DRY_RUN"}:
                    stats["saved_attachments"] += 1
                elif result == "SKIPPED_EXISTS":
                    stats["skipped_existing"] += 1

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
        # Body extraction is paused. Ask Aidan whether this tenant's body text
        # should be saved later, and confirm the exact body markers first.
        stats["no_file_attachments_pending_aidan"] += 1
        rows.append(
            report_row(
                message,
                config,
                "SKIPPED_NO_FILE_ATTACHMENTS_PENDING_AIDAN",
                note="Files-only historic download. Body processing is pending Aidan confirmation.",
            )
        )

    except Exception as error:
        stats["failed"] += 1
        rows.append(report_row(message, config, "FAILED_MESSAGE", note=str(error)))
        print(f"ERROR processing message {message.get('id')}: {error}")


# =============================================================================
# STEP 10 - MAIN SCRIPT FLOW
# =============================================================================

def run(args: argparse.Namespace) -> int:
    r"""
    Main execution flow.

    Main objective:
        Save inbound files into the correct tenant folder under:
        \\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Production\Integration_Layer

    Current confirmed example:
        BKD files are saved into:
        \\PL-AZ-SDF-PLINT\Fusion_Production\Synovia_Flow_Production\Integration_Layer\BKD\Inbound\Sales_Order_files

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

    print("STEP 1 - Checking Graph configuration")
    check_graph_configuration()

    print("STEP 2 - Getting Microsoft Graph token")
    token = get_graph_token()

    print("STEP 3 - Reading mailbox messages")
    messages = read_messages(token, args)
    print(f"Found {len(messages)} message(s) to review")

    print("STEP 4 - Processing messages by tenant rules")
    for message in messages:
        process_one_message(token, message, stats, rows, args)

    print("STEP 5 - Finalising support summary")
    run_log_path = write_run_log(rows, stats, started_at, args)

    print_summary(stats, run_log_path)
    return 1 if stats["failed"] else 0


def print_summary(stats: dict[str, Any], run_log_path: Path) -> None:
    """Print the final result in the terminal."""
    print("")
    print("Graph download finished")
    print(f"  Scanned messages: {stats['scanned']}")
    print(f"  Matched messages: {stats['matched']}")
    print(f"  Unmatched messages: {stats['unmatched']}")
    print(f"  Saved attachments: {stats['saved_attachments']}")
    print(f"  Skipped existing files: {stats['skipped_existing']}")
    print(
        "  No file attachments / body pending Aidan: "
        f"{stats['no_file_attachments_pending_aidan']}"
    )
    print(f"  Failed messages: {stats['failed']}")
    print(f"  Technical run log: {run_log_path}")

    if stats["file_types"]:
        print("  File types:")
        for suffix, count in sorted(stats["file_types"].items()):
            print(f"    {suffix}: {count}")


# =============================================================================
# STEP 11 - COMMAND LINE OPTIONS
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    """Define optional filters for historic downloads."""
    parser = argparse.ArgumentParser(
        description="Simple Microsoft Graph downloader by tenant sender domain."
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

    if args.max_messages < 0:
        parser.error("--max-messages cannot be negative")
    if args.page_size < 1 or args.page_size > 100:
        parser.error("--page-size must be between 1 and 100")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())

