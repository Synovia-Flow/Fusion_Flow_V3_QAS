from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import allowed_origins, config_value
from .db import (
    DbUnavailable,
    execute,
    execute_scalar,
    mirror_execute_scalar,
    mirror_query_all,
    mirror_query_one,
    query_all,
    query_one,
)
from .file_introspection import inspect_upload, summarise_mapping
from .login_audit import EVENT_LOGIN_FAILURE, EVENT_LOGIN_SUCCESS, EVENT_LOGOUT, record_login_audit
from .mapping_suggestions import suggest_column_mappings
from .upload_processing_preview import build_processing_preview, revalidate_processing_preview
from Modules.Processing.preview_enrichment import PACKAGE_TYPE_RESOLUTION_ORDER, product_master_lookup, product_master_summary
from Modules.Processing.validation_summary import build_consignment_validation_summary
from .tenant_schema import (
    UnknownTenant,
    consignment_id_expression,
    consignment_loaded_at_expression,
    ens_header_id_expression,
    ens_header_loaded_at_expression,
    known_tenants,
    mirror_has_column,
    tenant_has_sfd,
    tenant_schema,
)
from .tss_api_dates import TSS_API_DATE_RANGE_OPTIONS, normalise_tss_api_date_range, tss_api_date_bounds
from .tss_profiles import fallback_profile, fallback_profiles, normalize_portal_code, portal_code_for_tss_client, required_file_ordinal, select_required_file
from .tss_submission import build_consignment_submission_plan, post_tss_json

app = FastAPI(
    title="Fusion Flow Portal API",
    version="0.1.0",
    description="Read API for Fusion Flow V3 QAS CFG/ING/PRS portal routes.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def client_code_param(client_code: str) -> str:
    portal_code = normalize_portal_code(client_code)
    if len(portal_code) != 3 or not portal_code.isalnum():
        raise HTTPException(status_code=422, detail="client_code must be a known portal/client code.")
    profile = fallback_profile(portal_code)
    if profile:
        return str(profile["clientCode"])
    return portal_code


def safe_limit(value: int, default: int = 100, maximum: int = 500) -> int:
    if value <= 0:
        return default
    return min(value, maximum)


def db_error(exc: DbUnavailable) -> HTTPException:
    return HTTPException(status_code=503, detail={"message": str(exc), "db_available": False})


def table_exists(table_name: str) -> bool:
    return bool(execute_scalar("SELECT OBJECT_ID(?, 'U')", [table_name]))


def object_exists(object_name: str) -> bool:
    try:
        return bool(execute_scalar("SELECT OBJECT_ID(?)", [object_name]))
    except DbUnavailable:
        raise
    except Exception:
        return False


def safe_count(table_name: str, where: str = "", params: list[object] | None = None) -> int:
    if not object_exists(table_name):
        return 0
    try:
        return int(execute_scalar(f"SELECT COUNT(*) FROM {table_name}{where}", params or []) or 0)
    except DbUnavailable:
        raise
    except Exception:
        return 0


def safe_rows(object_name: str, sql: str, params: list[object] | None = None) -> list[dict[str, object]]:
    if not object_exists(object_name):
        return []
    try:
        return query_all(sql, params or [])
    except DbUnavailable:
        raise
    except Exception:
        return []


def status_vocabulary_rows() -> list[dict[str, object]]:
    return safe_rows(
        "CFG.Status_Vocabulary",
        """
        SELECT ProcessName, ResultStatus, Meaning, SortOrder,
               CAST(IsTerminal AS int) AS IsTerminal,
               CAST(IsException AS int) AS IsException
        FROM CFG.Status_Vocabulary
        ORDER BY SortOrder, ResultStatus
        """,
    )


def fallback_route(profile: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "stepNo": 1,
            "operationCode": "UPDATE_CONSIGNMENT_WITH_ENS",
            "resourceName": "Consignment",
            "endpoint": "/consignments",
            "httpMethod": "POST",
            "opType": "update",
            "requiresPrevious": None,
            "notes": "ENS/declaration_number must be present before submit.",
        },
        {
            "stepNo": 2,
            "operationCode": "SUBMIT_CONSIGNMENT",
            "resourceName": "Consignment",
            "endpoint": "/consignments",
            "httpMethod": "POST",
            "opType": "submit",
            "requiresPrevious": "UPDATE_CONSIGNMENT_WITH_ENS",
            "notes": "Submit only after the ENS update step.",
        },
    ]


def load_portal_profile(value: str) -> dict[str, object]:
    portal_code = normalize_portal_code(value)
    profile = fallback_profile(portal_code)
    if profile:
        profile["dbBacked"] = False
    if not profile:
        raise HTTPException(status_code=404, detail=f"No portal profile configured for {value}.")

    try:
        client = query_one(
            """
            SELECT ClientCode, ClientName, SchemaName, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes
            FROM CFG.Clients
            WHERE ClientCode = ?
            """,
            [profile["clientCode"]],
        )
        if client:
            profile["dbBacked"] = True
            profile["clientCode"] = client["ClientCode"]
            profile["clientName"] = client.get("ClientName") or profile["clientName"]
            profile["schemaName"] = client.get("SchemaName")
            profile["defaultRoute"] = client.get("DefaultRoute")
            profile["isAgent"] = bool(client.get("IsAgent"))
            profile["actAsSysId"] = client.get("ActAsSysId")
            profile["isActive"] = bool(client.get("IsActive"))
            profile["notes"] = client.get("Notes") or profile.get("notes")

        credential = query_one(
            """
            SELECT TOP 1 ClientCode, EnvCode
            FROM CFG.TSS_Credential
            WHERE ClientCode = ?
            ORDER BY IsActive DESC, CASE WHEN EnvCode IN ('PRD', 'TST') THEN 0 ELSE 1 END, EnvCode
            """,
            [profile["tssCredentialClientCode"]],
        )
        if credential:
            profile["dbBacked"] = True
            profile["tssCredentialClientCode"] = credential["ClientCode"]
            profile["preferredEnvCode"] = credential["EnvCode"]
    except DbUnavailable:
        raise
    except Exception:
        pass

    return profile



def load_preview_product_master(profile: dict[str, object]) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    context: dict[str, object] = {"source": "CFG.Product_Master", "available": False, "rowCount": 0}
    if not profile.get("dbBacked"):
        context["reason"] = "Profile is not DB-backed; preview ran without CFG product master enrichment."
        return {}, context

    client_code = str(profile.get("clientCode") or "").upper()
    if not client_code:
        context["reason"] = "Client code is missing; preview ran without CFG product master enrichment."
        return {}, context

    try:
        rows = query_all(
            """
            SELECT ClientCode, CustomerCode, SKU, ProductCode, ProductName, GoodsDescription,
                   CommodityCode, CountryOfOrigin, PackageType, PackageMarks,
                   ProcedureCode, AdditionalProcedureCode, ValuationMethod,
                   PreferenceCode, NiAdditionalInfoCode, NatureOfTransaction,
                   GrossWeightKg, NetWeightKg, UnitValue, Currency, ControlledGoods
            FROM CFG.Product_Master
            WHERE IsActive = 1 AND ClientCode IN (?, 'ALL')
            ORDER BY CASE WHEN ClientCode = ? THEN 0 ELSE 1 END,
                     CASE WHEN NULLIF(LTRIM(RTRIM(COALESCE(CustomerCode, ''))), '') IS NULL THEN 1 WHEN CustomerCode = 'ALL' THEN 2 ELSE 0 END,
                     ProductCode, SKU
            """,
            [client_code, client_code],
        )
    except DbUnavailable:
        raise
    except Exception:
        context["reason"] = "CFG.Product_Master lookup failed; preview ran without product master enrichment."
        return {}, context

    context["available"] = True
    context.update(product_master_summary(rows))
    if context.get("packageTypeWarning"):
        context["warning"] = context["packageTypeWarning"]
    return product_master_lookup(rows), context


def load_submission_route(profile: dict[str, object]) -> list[dict[str, object]]:
    try:
        rows = query_all(
            """
            SELECT StepNo, ResourceName, Endpoint, HttpMethod, OpType, WaitSeconds, Notes
            FROM CFG.API_Process_Map
            WHERE ClientCode = ? AND RouteCode = COALESCE(?, RouteCode) AND IsActive = 1
            ORDER BY StepNo
            """,
            [profile["clientCode"], profile.get("defaultRoute")],
        )
        if rows:
            return [
                {
                    "stepNo": row["StepNo"],
                    "operationCode": f"{str(row.get('ResourceName') or 'TSS').upper().replace(' ', '_')}_{str(row.get('OpType') or '').upper()}",
                    "resourceName": row["ResourceName"],
                    "endpoint": row["Endpoint"],
                    "httpMethod": row["HttpMethod"],
                    "opType": row.get("OpType"),
                    "requiresPrevious": None,
                    "waitSeconds": row.get("WaitSeconds"),
                    "notes": row.get("Notes"),
                }
                for row in rows
            ]
    except DbUnavailable:
        raise
    except Exception:
        pass
    return fallback_route(profile)


def credential_status(profile: dict[str, object], env_code: str | None = None, include_secret: bool = False) -> dict[str, object] | None:
    env = (env_code or str(profile["preferredEnvCode"])).upper()
    row = query_one(
        """
        SELECT c.ClientCode AS CredentialClientCode, c.EnvCode, c.TssUsername,
               CASE WHEN c.TssPassword IS NULL OR c.TssPassword = '' THEN 0 ELSE 1 END AS HasPassword,
               c.TssPassword, c.IsActive, c.LastVerified, c.LastStatus, c.HttpStatus,
               e.BaseUrl, e.EnvName
        FROM CFG.TSS_Credential c
        LEFT JOIN CFG.TSS_Environment e ON e.EnvCode = c.EnvCode
        WHERE c.ClientCode = ? AND c.EnvCode = ?
        """,
        [profile["tssCredentialClientCode"], env],
    )
    if not row:
        return None
    payload: dict[str, object] = {
        "credentialClientCode": row["CredentialClientCode"],
        "envCode": row["EnvCode"],
        "envName": row.get("EnvName"),
        "baseUrl": row.get("BaseUrl"),
        "tssUsername": row["TssUsername"],
        "hasPassword": bool(row["HasPassword"]),
        "isActive": bool(row["IsActive"]),
        "lastVerified": row.get("LastVerified"),
        "lastStatus": row.get("LastStatus"),
        "httpStatus": row.get("HttpStatus"),
    }
    if include_secret:
        payload["password"] = row.get("TssPassword")
    return payload


def classify_tss_status(status: int | None) -> str:
    if status is None:
        return "ERROR"
    if 200 <= status < 300:
        return "PASS"
    if status in (401, 403):
        return "AUTH_FAILED"
    if 400 <= status < 500:
        return "REQUEST_FAILED"
    if status >= 500:
        return "TSS_ERROR"
    return "ERROR"


def describe_tss_test_result(result: str, status: int | None) -> tuple[str, str]:
    if result == "PASS":
        return "success", "TSS API check passed."
    if result == "AUTH_FAILED":
        return "error", "TSS answered, but the configured credentials were rejected."
    if result == "REQUEST_FAILED":
        return "warning", f"TSS answered with HTTP {status}; the test request did not pass."
    if result == "TSS_ERROR":
        return "error", f"TSS answered with HTTP {status}; the remote service returned an error."
    return "error", "No valid response was received from TSS."


def load_consignment_submission_data(consignment_row_id: int, profile: dict[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    row = query_one(
        """
        SELECT c.*, h.declaration_number AS HeaderDeclarationNumber,
               h.arrival_date_time AS HeaderArrivalDateTime, h.MovementKey AS HeaderMovementKey
        FROM PRS.Consignment c
        LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
        WHERE c.ConsignmentRowID = ? AND c.ClientCode = ?
        """,
        [consignment_row_id, profile["clientCode"]],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Consignment was not found for this portal/client profile.")
    goods = query_all(
        """
        SELECT TOP 100 *
        FROM PRS.Goods_Item
        WHERE ConsignmentRowID = ?
        ORDER BY GoodsItemOrdinal, GoodsItemRowID
        """,
        [consignment_row_id],
    )
    return row, goods


def public_credential_payload(credential: dict[str, object] | None) -> dict[str, object] | None:
    if not credential:
        return None
    return {key: value for key, value in credential.items() if key != "password"}


def settings_row(
    key: str,
    label: str,
    value: object = "",
    description: str = "",
    source_table: str = "CFG.Application_Parameters",
    input_type: str = "text",
    updated_at: object | None = None,
    choices: list[dict[str, str]] | None = None,
    is_secret: bool = False,
    placeholder: str = "",
    editable: bool = True,
) -> dict[str, object]:
    return {
        "key": key,
        "label": label,
        "value": "" if value is None else str(value),
        "description": description,
        "sourceTable": source_table,
        "inputType": input_type,
        "updatedAt": updated_at,
        "choices": choices or [],
        "isSecret": is_secret,
        "placeholder": placeholder,
        "editable": editable,
    }


def settings_section(section_id: str, label: str, icon: str, description: str, rows: list[dict[str, object]]) -> dict[str, object]:
    return {"id": section_id, "label": label, "icon": icon, "description": description, "rows": rows}


def application_parameter_map(keys: list[str]) -> dict[str, dict[str, object]]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = query_all(
        f"""
        SELECT ParameterKey, ParameterValue, ValueType, IsActive, UpdatedAt
        FROM CFG.Application_Parameters
        WHERE ParameterKey IN ({placeholders})
        """,
        keys,
    )
    return {str(row["ParameterKey"]): row for row in rows}


def parameter_value(params: dict[str, dict[str, object]], key: str) -> str:
    row = params.get(key)
    if not row:
        return ""
    if str(row.get("ValueType") or "").upper() == "SECRET":
        return ""
    return str(row.get("ParameterValue") or "")


def parameter_updated_at(params: dict[str, dict[str, object]], key: str) -> object | None:
    return params.get(key, {}).get("UpdatedAt")


def parse_config_json(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def admin_settings_payload(profile: dict[str, object]) -> dict[str, object]:
    app_keys = [
        "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID", "GRAPH_CLIENT_SECRET", "GRAPH_MAILBOX",
        "GRAPH_PROCESSED_FOLDER", "GRAPH_FORWARDERS", "PROCESSING_CLIENT", "PROCESSING_DRY_RUN",
        "PROCESSING_TRANSACTION_MODE", "ARRIVAL_MAX_FUTURE_DAYS", "API_RATE_LIMIT_SECONDS", "DEFAULT_ENV", "SDI_DEADLINE_DAY",
        "NOTIFY_ENS_RECEIVED_ENABLED", "NOTIFY_CONSIGNMENTS_RECEIVED_ENABLED",
        "NOTIFY_STAGING_FAILURES_ENABLED", "NOTIFY_MOVEMENT_AUTHORISED_ENABLED",
        "NOTIFY_STAGING_FAILURES_TO", "NOTIFY_MOVEMENT_AUTHORISED_TO", "NOTIFY_ENS_PACK_AUTO_TO",
    ]
    params = application_parameter_map(app_keys)
    environments = query_all(
        """
        SELECT EnvCode, EnvName, BaseUrl, IsActive
        FROM CFG.TSS_Environment
        WHERE EnvCode IN ('PRD', 'TST')
        """
    )
    env_by_code = {str(row["EnvCode"]): row for row in environments}
    credential = credential_status(profile)
    folder_rows = query_all(
        """
        SELECT PathType, PathValue, IsActive, UpdatedAt
        FROM CFG.Folder_Paths
        WHERE ClientCode = ?
        ORDER BY PathType
        """,
        [profile["clientCode"]],
    )
    folders = {str(row["PathType"]): row for row in folder_rows}
    source_email = query_one(
        """
        SELECT TOP 1 Channel, IsActive, ProcessedSubfolder, ConfigJson, UpdatedAt
        FROM CFG.Ingestion_Source
        WHERE ClientCode = ? AND Channel = 'EMAIL'
        """,
        [profile["clientCode"]],
    ) or {}
    source_config = parse_config_json(source_email.get("ConfigJson"))
    file_selection = profile.get("fileSelection") or {}

    tss_rows = [
        settings_row("BASE_URL", "Production URL", env_by_code.get("PRD", {}).get("BaseUrl"), "Production TSS API base URL.", "CFG.TSS_Environment", "url"),
        settings_row("TEST_URL", "Test URL", env_by_code.get("TST", {}).get("BaseUrl"), "Test/QAS TSS API base URL.", "CFG.TSS_Environment", "url"),
        settings_row("ENVIRONMENT", "Environment", profile.get("preferredEnvCode"), "Portal mode or active TSS target for this tenant. Demo is preview-only; Production/Test activate the matching CFG.TSS_Credential row.", "CFG.TSS_Credential", "select", choices=[{"value": "DEMO", "label": "Demo"}, {"value": "PRD", "label": "Production"}, {"value": "TST", "label": "Test/QAS"}]),
        settings_row("USERNAME", "User", (credential or {}).get("tssUsername"), "TSS API username for this tenant.", "CFG.TSS_Credential"),
        settings_row("PASSWORD", "Password", "", "TSS API password for this tenant.", "CFG.TSS_Credential", "password", is_secret=True, placeholder="Configured" if (credential or {}).get("hasPassword") else "Not configured"),
        settings_row("ACT_AS", "Act as", profile.get("actAsSysId"), "Optional customer_account_sys_id for delegated TSS calls.", "CFG.Clients"),
    ]

    graph_rows = [
        settings_row("ENABLED", "Enabled", "true" if source_email.get("IsActive") else "false", "Microsoft Graph mailbox polling for inbound messages.", "CFG.Ingestion_Source", "boolean", source_email.get("UpdatedAt")),
        settings_row("TENANT_ID", "Tenant ID", "", "Microsoft Entra tenant id used by Graph.", "CFG.Application_Parameters", "password", parameter_updated_at(params, "GRAPH_TENANT_ID"), is_secret=True, placeholder="Configured" if parameter_value(params, "GRAPH_TENANT_ID") else "Not configured"),
        settings_row("CLIENT_ID", "Client ID", "", "Microsoft Graph application client id.", "CFG.Application_Parameters", "password", parameter_updated_at(params, "GRAPH_CLIENT_ID"), is_secret=True, placeholder="Configured" if parameter_value(params, "GRAPH_CLIENT_ID") else "Not configured"),
        settings_row("CLIENT_SECRET", "Client secret", "", "Microsoft Graph application client secret.", "CFG.Application_Parameters", "password", parameter_updated_at(params, "GRAPH_CLIENT_SECRET"), is_secret=True, placeholder="Configured" if params.get("GRAPH_CLIENT_SECRET") else "Not configured"),
        settings_row("MAILBOX", "Mailbox", parameter_value(params, "GRAPH_MAILBOX"), "Mailbox UPN or email address to poll.", "CFG.Application_Parameters", "email", parameter_updated_at(params, "GRAPH_MAILBOX")),
        settings_row("FOLDER", "Folder", source_config.get("folder", "INBOX"), "Graph folder id, well-known name, or root display name.", "CFG.Ingestion_Source"),
        settings_row("PROCESSED_FOLDER", "Processed folder", parameter_value(params, "GRAPH_PROCESSED_FOLDER"), "Folder used after successful processing.", "CFG.Application_Parameters", "text", parameter_updated_at(params, "GRAPH_PROCESSED_FOLDER")),
        settings_row("ALLOWED_SENDER_DOMAINS", "Allowed sender domains", source_config.get("sender_domain", ""), "Comma-separated sender domains allowed for Graph ingestion.", "CFG.Ingestion_Source"),
    ]

    ingestion_rows = [
        settings_row("ENABLED", "Email source enabled", "true" if source_email.get("IsActive") else "false", "Enables the EMAIL ingestion source for this tenant.", "CFG.Ingestion_Source", "boolean", source_email.get("UpdatedAt")),
        settings_row("PROCESSED_SUBFOLDER", "Processed subfolder", source_email.get("ProcessedSubfolder"), "Subfolder label used after processing.", "CFG.Ingestion_Source"),
        settings_row("ATTACHMENT_TO_MAP", "Attachment to map", file_selection.get("requiredFileOrdinal"), "Attached file ordinal that becomes the consignment input. Currently owned by portal bridge code.", "portal_bridge", "number", editable=False),
        settings_row("TARGET_RAW", "Raw target", file_selection.get("targetLandingTable"), "Landing tables used by upload preview.", "portal_bridge", editable=False),
        *[
            settings_row(path_type, path_type.replace("_", " ").title(), folders.get(path_type, {}).get("PathValue"), f"Operational {path_type.lower()} folder.", "CFG.Folder_Paths", "text", folders.get(path_type, {}).get("UpdatedAt"))
            for path_type in ("INBOUND", "PROCESS", "FAIL", "ARCHIVE", "ENS_SOURCE")
            if path_type in folders
        ],
    ]

    validation_rows = [
        settings_row("PROCESSING_CLIENT", "Processing client", parameter_value(params, "PROCESSING_CLIENT"), "Client selected for Module 2 processing.", "CFG.Application_Parameters", "text", parameter_updated_at(params, "PROCESSING_CLIENT")),
        settings_row("PROCESSING_DRY_RUN", "Processing dry run", "true" if parameter_value(params, "PROCESSING_DRY_RUN") in ("1", "true", "TRUE") else "false", "When true, processing avoids final mutations.", "CFG.Application_Parameters", "boolean", parameter_updated_at(params, "PROCESSING_DRY_RUN")),
        settings_row("PROCESSING_TRANSACTION_MODE", "Transaction mode", parameter_value(params, "PROCESSING_TRANSACTION_MODE"), "Module 2 transaction selection mode.", "CFG.Application_Parameters", "select", parameter_updated_at(params, "PROCESSING_TRANSACTION_MODE"), choices=[{"value": "latest", "label": "latest"}, {"value": "all", "label": "all"}]),
        settings_row("ARRIVAL_MAX_FUTURE_DAYS", "Arrival future days", parameter_value(params, "ARRIVAL_MAX_FUTURE_DAYS"), "Maximum arrival-date future window accepted by validation.", "CFG.Application_Parameters", "number", parameter_updated_at(params, "ARRIVAL_MAX_FUTURE_DAYS")),
        settings_row("API_RATE_LIMIT_SECONDS", "API rate limit", parameter_value(params, "API_RATE_LIMIT_SECONDS"), "Delay between outbound TSS API calls.", "CFG.Application_Parameters", "number", parameter_updated_at(params, "API_RATE_LIMIT_SECONDS")),
    ]

    sdi_rows = [
        settings_row("SDI_DEADLINE_DAY", "SDI deadline day", parameter_value(params, "SDI_DEADLINE_DAY"), "Day-of-month control used by SDI deadline automation.", "CFG.Application_Parameters", "number", parameter_updated_at(params, "SDI_DEADLINE_DAY")),
    ]

    notification_rows = [
        settings_row("ENS_RECEIVED_ENABLED", "ENS received", parameter_value(params, "NOTIFY_ENS_RECEIVED_ENABLED") or "false", "Notify operators after an ENS source has landed successfully.", "CFG.Application_Parameters", "boolean", parameter_updated_at(params, "NOTIFY_ENS_RECEIVED_ENABLED")),
        settings_row("CONSIGNMENTS_RECEIVED_ENABLED", "Consignments received", parameter_value(params, "NOTIFY_CONSIGNMENTS_RECEIVED_ENABLED") or "false", "Notify operators after a consignment pack has landed successfully.", "CFG.Application_Parameters", "boolean", parameter_updated_at(params, "NOTIFY_CONSIGNMENTS_RECEIVED_ENABLED")),
        settings_row("STAGING_FAILURES_ENABLED", "Staging failures", parameter_value(params, "NOTIFY_STAGING_FAILURES_ENABLED") or "false", "Notify operators when staging needs manual action.", "CFG.Application_Parameters", "boolean", parameter_updated_at(params, "NOTIFY_STAGING_FAILURES_ENABLED")),
        settings_row("MOVEMENT_AUTHORISED_ENABLED", "Movement authorised", parameter_value(params, "NOTIFY_MOVEMENT_AUTHORISED_ENABLED") or "false", "Notify operators after mirrored TSS status confirms authorised for movement.", "CFG.Application_Parameters", "boolean", parameter_updated_at(params, "NOTIFY_MOVEMENT_AUTHORISED_ENABLED")),
        settings_row("STAGING_FAILURES_TO", "Staging failure recipients", parameter_value(params, "NOTIFY_STAGING_FAILURES_TO"), "Comma-separated operational recipients for staging failures.", "CFG.Application_Parameters", "text", parameter_updated_at(params, "NOTIFY_STAGING_FAILURES_TO")),
        settings_row("MOVEMENT_AUTHORISED_TO", "Movement authorised recipients", parameter_value(params, "NOTIFY_MOVEMENT_AUTHORISED_TO"), "Comma-separated recipients for authorised movement notices.", "CFG.Application_Parameters", "text", parameter_updated_at(params, "NOTIFY_MOVEMENT_AUTHORISED_TO")),
        settings_row("ENS_PACK_AUTO_TO", "ENS pack recipients", parameter_value(params, "NOTIFY_ENS_PACK_AUTO_TO"), "Comma-separated recipients for the optional ENS movement pack.", "CFG.Application_Parameters", "text", parameter_updated_at(params, "NOTIFY_ENS_PACK_AUTO_TO")),
    ]

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "clientName": profile["clientName"],
        "source": "CFG.Application_Parameters + CFG.Folder_Paths + CFG.Ingestion_Source + CFG.TSS_*",
        "writeMode": "db_write_existing_cfg",
        "sections": [
            settings_section("TSS_API", "TSS Portal API", "sync_alt", "Credentials and endpoints for Trader Support Service.", tss_rows),
            settings_section("GRAPH", "Inbound Email / Microsoft Graph", "mail", "Inbound mailbox pickup using Microsoft Graph application credentials.", graph_rows),
            settings_section("INGEST_AUTO", "Ingestion & Folders", "drive_folder_upload", "Inbound source, attachment-selection and operational folders.", ingestion_rows),
            settings_section("SDI_AUTO", "SDI / SupDec Automation", "bolt", "Supplementary declaration automation controls currently present in CFG.", sdi_rows),
            settings_section("VALIDATION", "Validation Controls", "shield", "Runtime switches that control local validation before TSS.", validation_rows),
            settings_section("NOTIFY", "Email Automation Notifications", "notifications", "V2-style notification controls stored in existing CFG parameters. Enabling a switch does not create a scheduler.", notification_rows),
        ],
    }


APP_PARAMETER_WRITE_MAP = {
    ("GRAPH", "TENANT_ID"): ("GRAPH_TENANT_ID", "SECRET", "Microsoft Entra tenant id used by Graph."),
    ("GRAPH", "CLIENT_ID"): ("GRAPH_CLIENT_ID", "SECRET", "Microsoft Graph application client id."),
    ("GRAPH", "CLIENT_SECRET"): ("GRAPH_CLIENT_SECRET", "SECRET", "Microsoft Graph application client secret."),
    ("GRAPH", "MAILBOX"): ("GRAPH_MAILBOX", "STRING", "Mailbox UPN or email address to poll."),
    ("GRAPH", "PROCESSED_FOLDER"): ("GRAPH_PROCESSED_FOLDER", "STRING", "Folder used after successful processing."),
    ("VALIDATION", "PROCESSING_CLIENT"): ("PROCESSING_CLIENT", "STRING", "Client selected for Module 2 processing."),
    ("VALIDATION", "PROCESSING_DRY_RUN"): ("PROCESSING_DRY_RUN", "BOOLEAN", "When true, processing avoids final mutations."),
    ("VALIDATION", "PROCESSING_TRANSACTION_MODE"): ("PROCESSING_TRANSACTION_MODE", "STRING", "Module 2 transaction selection mode."),
    ("VALIDATION", "ARRIVAL_MAX_FUTURE_DAYS"): ("ARRIVAL_MAX_FUTURE_DAYS", "INTEGER", "Maximum arrival-date future window accepted by validation."),
    ("VALIDATION", "API_RATE_LIMIT_SECONDS"): ("API_RATE_LIMIT_SECONDS", "INTEGER", "Delay between outbound TSS API calls."),
    ("SDI_AUTO", "SDI_DEADLINE_DAY"): ("SDI_DEADLINE_DAY", "INTEGER", "Day-of-month control used by SDI deadline automation."),
    ("NOTIFY", "ENS_RECEIVED_ENABLED"): ("NOTIFY_ENS_RECEIVED_ENABLED", "BOOLEAN", "Notify operators after an ENS source lands."),
    ("NOTIFY", "CONSIGNMENTS_RECEIVED_ENABLED"): ("NOTIFY_CONSIGNMENTS_RECEIVED_ENABLED", "BOOLEAN", "Notify operators after a consignment pack lands."),
    ("NOTIFY", "STAGING_FAILURES_ENABLED"): ("NOTIFY_STAGING_FAILURES_ENABLED", "BOOLEAN", "Notify operators about staging failures."),
    ("NOTIFY", "MOVEMENT_AUTHORISED_ENABLED"): ("NOTIFY_MOVEMENT_AUTHORISED_ENABLED", "BOOLEAN", "Notify operators after TSS authorises a movement."),
    ("NOTIFY", "STAGING_FAILURES_TO"): ("NOTIFY_STAGING_FAILURES_TO", "STRING", "Operational recipients for staging failure notices."),
    ("NOTIFY", "MOVEMENT_AUTHORISED_TO"): ("NOTIFY_MOVEMENT_AUTHORISED_TO", "STRING", "Recipients for authorised movement notices."),
    ("NOTIFY", "ENS_PACK_AUTO_TO"): ("NOTIFY_ENS_PACK_AUTO_TO", "STRING", "Recipients for the optional ENS movement pack."),
}
SOURCE_BOOLEAN_KEYS = {("GRAPH", "ENABLED"), ("INGEST_AUTO", "ENABLED")}
SOURCE_CONFIG_KEYS = {
    ("GRAPH", "FOLDER"): "folder",
    ("GRAPH", "ALLOWED_SENDER_DOMAINS"): "sender_domain",
}
FOLDER_PATH_KEYS = {"INBOUND", "PROCESS", "FAIL", "ARCHIVE", "ENS_SOURCE"}
SECRET_UPDATE_KEYS = {("TSS_API", "PASSWORD"), ("GRAPH", "TENANT_ID"), ("GRAPH", "CLIENT_ID"), ("GRAPH", "CLIENT_SECRET")}
READ_ONLY_SETTING_KEYS = {
    ("INGEST_AUTO", "ATTACHMENT_TO_MAP"),
    ("INGEST_AUTO", "TARGET_RAW"),
}


def normalize_bool_value(value: object) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disabled"}:
        return False
    raise HTTPException(status_code=422, detail=f"Boolean setting value is invalid: {value}")


def clean_setting_value(value: object) -> str:
    return str(value or "").strip()


def upsert_application_parameter(key: str, value: str, value_type: str, description: str) -> int:
    return execute(
        """
        MERGE CFG.Application_Parameters AS target
        USING (SELECT ? AS ParameterKey) AS source
           ON target.ParameterKey = source.ParameterKey
        WHEN MATCHED THEN
            UPDATE SET ParameterValue = ?, ValueType = ?, Description = COALESCE(NULLIF(?, ''), Description), IsActive = 1, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ParameterKey, ParameterValue, ValueType, Description, IsActive)
            VALUES (?, ?, ?, NULLIF(?, ''), 1);
        """,
        [key, value, value_type, description, key, value, value_type, description],
    )


def ensure_email_source(profile: dict[str, object]) -> None:
    execute(
        """
        MERGE CFG.Ingestion_Source AS target
        USING (SELECT ? AS ClientCode, 'EMAIL' AS Channel) AS source
           ON target.ClientCode = source.ClientCode AND target.Channel = source.Channel
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, Channel, IsActive, ProcessedSubfolder, ConfigJson, Notes)
            VALUES (?, 'EMAIL', 0, 'Processed', '{}', 'Created by Fusion Portal settings');
        """,
        [profile["clientCode"], profile["clientCode"]],
    )


def update_email_source_config(profile: dict[str, object], key: str, value: str) -> int:
    ensure_email_source(profile)
    row = query_one(
        """
        SELECT ConfigJson
        FROM CFG.Ingestion_Source
        WHERE ClientCode = ? AND Channel = 'EMAIL'
        """,
        [profile["clientCode"]],
    ) or {}
    config = parse_config_json(row.get("ConfigJson"))
    config[key] = value
    return execute(
        """
        UPDATE CFG.Ingestion_Source
        SET ConfigJson = ?, UpdatedAt = SYSUTCDATETIME()
        WHERE ClientCode = ? AND Channel = 'EMAIL'
        """,
        [json.dumps(config, ensure_ascii=False, sort_keys=True), profile["clientCode"]],
    )


def upsert_folder_path(profile: dict[str, object], path_type: str, value: str) -> int:
    if not value:
        raise HTTPException(status_code=422, detail=f"{path_type} folder path cannot be blank.")
    return execute(
        """
        MERGE CFG.Folder_Paths AS target
        USING (SELECT ? AS ClientCode, ? AS PathType) AS source
           ON target.ClientCode = source.ClientCode AND target.PathType = source.PathType
        WHEN MATCHED THEN
            UPDATE SET PathValue = ?, IsActive = 1, UpdatedAt = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN
            INSERT (ClientCode, PathType, PathValue, IsActive)
            VALUES (?, ?, ?, 1);
        """,
        [profile["clientCode"], path_type, value, profile["clientCode"], path_type, value],
    )


def save_admin_settings_payload(profile: dict[str, object], updates: object) -> dict[str, object]:
    if not isinstance(updates, list):
        raise HTTPException(status_code=422, detail="updates must be a list.")

    saved: list[str] = []
    ignored: list[str] = []
    tss_client = str(profile.get("tssCredentialClientCode") or profile["clientCode"])
    preferred_env = str(profile.get("preferredEnvCode") or "PRD")

    for item in updates:
        if not isinstance(item, dict):
            ignored.append("invalid")
            continue
        section = clean_setting_value(item.get("sectionId") or item.get("section") or item.get("category")).upper()
        key = clean_setting_value(item.get("key")).upper()
        value = clean_setting_value(item.get("value"))
        setting_id = f"{section}.{key}"

        if not section or not key:
            ignored.append("missing-key")
            continue
        if (section, key) in READ_ONLY_SETTING_KEYS:
            ignored.append(setting_id)
            continue
        if (section, key) in SECRET_UPDATE_KEYS and not value:
            ignored.append(setting_id)
            continue

        if (section, key) in APP_PARAMETER_WRITE_MAP:
            param_key, value_type, description = APP_PARAMETER_WRITE_MAP[(section, key)]
            if value_type == "BOOLEAN":
                value = "true" if normalize_bool_value(value) else "false"
            if (section, key) == ("VALIDATION", "PROCESSING_TRANSACTION_MODE") and value not in {"latest", "all"}:
                raise HTTPException(status_code=422, detail="PROCESSING_TRANSACTION_MODE must be latest or all.")
            upsert_application_parameter(param_key, value, value_type, description)
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "BASE_URL"):
            if not value:
                raise HTTPException(status_code=422, detail="TSS production URL cannot be blank.")
            execute("UPDATE CFG.TSS_Environment SET BaseUrl = ? WHERE EnvCode = 'PRD'", [value])
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "TEST_URL"):
            if not value:
                raise HTTPException(status_code=422, detail="TSS test URL cannot be blank.")
            execute("UPDATE CFG.TSS_Environment SET BaseUrl = ? WHERE EnvCode = 'TST'", [value])
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "ENVIRONMENT"):
            selected_env = value.upper()
            if selected_env == "DEMO":
                ignored.append(setting_id)
                continue
            if selected_env not in {"PRD", "TST"}:
                raise HTTPException(status_code=422, detail="TSS environment must be DEMO, PRD or TST.")
            credential_exists = query_one(
                """
                SELECT TOP 1 1 AS Found
                FROM CFG.TSS_Credential
                WHERE ClientCode = ? AND EnvCode = ?
                """,
                [tss_client, selected_env],
            )
            if not credential_exists:
                raise HTTPException(status_code=409, detail="No CFG.TSS_Credential row exists for this tenant/environment.")
            execute(
                """
                UPDATE CFG.TSS_Credential
                SET IsActive = CASE WHEN EnvCode = ? THEN 1 ELSE 0 END,
                    UpdatedAt = SYSUTCDATETIME()
                WHERE ClientCode = ? AND EnvCode IN ('PRD', 'TST')
                """,
                [selected_env, tss_client],
            )
            preferred_env = selected_env
            profile["preferredEnvCode"] = selected_env
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "USERNAME"):
            if not value:
                raise HTTPException(status_code=422, detail="TSS username cannot be blank.")
            count = execute(
                """
                UPDATE CFG.TSS_Credential
                SET TssUsername = ?, UpdatedAt = SYSUTCDATETIME()
                WHERE ClientCode = ? AND EnvCode = ?
                """,
                [value, tss_client, preferred_env],
            )
            if count <= 0:
                raise HTTPException(status_code=409, detail="No CFG.TSS_Credential row exists for this tenant/environment.")
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "PASSWORD"):
            count = execute(
                """
                UPDATE CFG.TSS_Credential
                SET TssPassword = ?, UpdatedAt = SYSUTCDATETIME()
                WHERE ClientCode = ? AND EnvCode = ?
                """,
                [value, tss_client, preferred_env],
            )
            if count <= 0:
                raise HTTPException(status_code=409, detail="No CFG.TSS_Credential row exists for this tenant/environment.")
            saved.append(setting_id)
        elif (section, key) == ("TSS_API", "ACT_AS"):
            execute(
                "UPDATE CFG.Clients SET ActAsSysId = NULLIF(?, ''), UpdatedAt = SYSUTCDATETIME() WHERE ClientCode = ?",
                [value, profile["clientCode"]],
            )
            saved.append(setting_id)
        elif (section, key) in SOURCE_BOOLEAN_KEYS:
            ensure_email_source(profile)
            execute(
                """
                UPDATE CFG.Ingestion_Source
                SET IsActive = ?, UpdatedAt = SYSUTCDATETIME()
                WHERE ClientCode = ? AND Channel = 'EMAIL'
                """,
                [1 if normalize_bool_value(value) else 0, profile["clientCode"]],
            )
            saved.append(setting_id)
        elif (section, key) == ("INGEST_AUTO", "PROCESSED_SUBFOLDER"):
            ensure_email_source(profile)
            execute(
                """
                UPDATE CFG.Ingestion_Source
                SET ProcessedSubfolder = NULLIF(?, ''), UpdatedAt = SYSUTCDATETIME()
                WHERE ClientCode = ? AND Channel = 'EMAIL'
                """,
                [value, profile["clientCode"]],
            )
            saved.append(setting_id)
        elif (section, key) in SOURCE_CONFIG_KEYS:
            update_email_source_config(profile, SOURCE_CONFIG_KEYS[(section, key)], value)
            saved.append(setting_id)
        elif section == "INGEST_AUTO" and key in FOLDER_PATH_KEYS:
            upsert_folder_path(profile, key, value)
            saved.append(setting_id)
        else:
            ignored.append(setting_id)

    result = admin_settings_payload(load_portal_profile(str(profile["portalClientCode"])))
    result.update({
        "savedCount": len(saved),
        "savedSettings": saved,
        "ignoredSettings": ignored,
        "savedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    return result

def log_api_trace(client_code: str, step: dict[str, object], request_payload: dict[str, object], result: dict[str, object]) -> None:
    execute(
        """
        INSERT INTO LOG.API_Trace
            (ClientCode, ResourceName, Endpoint, HttpMethod, RequestJson, ResponseJson, StatusCode, DurationMs)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            client_code,
            str(step.get("operationCode") or "TSS"),
            str(step.get("endpoint") or ""),
            str(step.get("httpMethod") or "POST"),
            json.dumps(request_payload, ensure_ascii=False, default=str),
            str(result.get("responseText") or "")[:8000],
            result.get("statusCode"),
            result.get("durationMs"),
        ],
    )


def public_connection_payload(profile: dict[str, object], env_code: str | None = None) -> dict[str, object]:
    credential = credential_status(profile, env_code=env_code)
    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "clientName": profile["clientName"],
        "tssCredentialClientCode": profile["tssCredentialClientCode"],
        "preferredEnvCode": env_code or profile["preferredEnvCode"],
        "requiresEnsBeforeSubmit": profile["requiresEnsBeforeSubmit"],
        "fileSelection": profile["fileSelection"],
        "credential": credential,
        "route": load_submission_route(profile),
    }




def cfg_parameter_value(key: str, default: str = "") -> str:
    row = query_one(
        """
        SELECT ParameterValue
        FROM CFG.Application_Parameters
        WHERE ParameterKey = ? AND IsActive = 1
        """,
        [key],
    )
    return str((row or {}).get("ParameterValue") or default).strip()


def default_submission_env() -> str:
    return (cfg_parameter_value("SUBMISSION_ENV") or cfg_parameter_value("DEFAULT_ENV") or "TST").upper()

def tss_api_base_path() -> str:
    return cfg_parameter_value("SUBMISSION_API_BASE_PATH", "/x_fhmrc_tss_api/v1/tss_api")


def tss_choice_values_base_path() -> str:
    return cfg_parameter_value("CHOICE_VALUES_PATH", "/x_fhmrc_tss_api/v1/choice_values")


def normalize_tss_endpoint(endpoint: str) -> str:
    return "/" + str(endpoint or "").strip("/")


def join_tss_url(base_url: object, base_path: str, endpoint: str, query: dict[str, object] | None = None) -> str:
    url = str(base_url or "").rstrip("/") + normalize_tss_endpoint(base_path) + normalize_tss_endpoint(endpoint)
    clean_query = {
        key: value
        for key, value in (query or {}).items()
        if value is not None and str(value).strip() != ""
    }
    if clean_query:
        url += "?" + urllib.parse.urlencode(clean_query)
    return url


def api_operation_code(step: dict[str, object]) -> str:
    resource = str(step.get("ResourceName") or "TSS").upper().replace(" ", "_").replace(".", "")
    return f"{resource}_{str(step.get('OpType') or '').upper()}"


def tss_public_endpoint(step: dict[str, object]) -> str:
    endpoint = normalize_tss_endpoint(str(step.get("Endpoint") or ""))
    op_type = str(step.get("OpType") or "").lower()
    if endpoint == "/permission_grant":
        return "/api/tss/permission-grant"
    if endpoint == "/headers":
        return "/api/tss/headers"
    if endpoint == "/consignments" and op_type == "submit":
        return "/api/tss/consignments/submit"
    if endpoint == "/consignments":
        return "/api/tss/consignments"
    if endpoint == "/goods" and op_type == "update":
        return "/api/tss/goods/update"
    if endpoint == "/goods":
        return "/api/tss/goods"
    if endpoint == "/simplified_frontier_declarations":
        return "/api/tss/simplified-frontier-declarations"
    if endpoint == "/gvms_gmr" and op_type == "submit":
        return "/api/tss/gvms-gmr/submit"
    if endpoint == "/gvms_gmr":
        return "/api/tss/gvms-gmr"
    if endpoint == "/supplementary_declarations" and op_type == "submit":
        return "/api/tss/supplementary-declarations/submit"
    if endpoint == "/supplementary_declarations":
        return "/api/tss/supplementary-declarations"
    return "/api/tss/resource"


def load_tss_api_map(client_code: str = "BKD", route_code: str | None = None) -> list[dict[str, object]]:
    code = client_code_param(client_code)
    params: list[object] = [code]
    where = ["ClientCode = ?", "IsActive = 1"]
    if route_code:
        where.append("RouteCode = ?")
        params.append(route_code)
    rows = query_all(
        f"""
        SELECT MapID, ClientCode, RouteCode, StepNo, ResourceName, Endpoint, HttpMethod,
               OpType, WaitSeconds, Notes, UpdatedAt
        FROM CFG.API_Process_Map
        WHERE {' AND '.join(where)}
        ORDER BY RouteCode, StepNo, MapID
        """,
        params,
    )
    return [
        {
            **row,
            "operationCode": api_operation_code(row),
            "publicEndpoint": tss_public_endpoint(row),
        }
        for row in rows
    ]


def resolve_tss_step(
    client_code: str,
    endpoint: str,
    method: str,
    op_type: str | None = None,
    route_code: str | None = None,
) -> dict[str, object]:
    endpoint = normalize_tss_endpoint(endpoint)
    candidates = [
        step
        for step in load_tss_api_map(client_code, route_code=route_code)
        if normalize_tss_endpoint(str(step.get("Endpoint") or "")) == endpoint
        and str(step.get("HttpMethod") or "").upper() == method.upper()
    ]
    if op_type:
        candidates = [step for step in candidates if str(step.get("OpType") or "").lower() == op_type.lower()]
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=f"No active CFG.API_Process_Map step for {method.upper()} {endpoint}"
            + (f" op_type={op_type}" if op_type else ""),
        )
    return candidates[0]



def load_tss_client_profile(value: str, env_code: str | None = None) -> dict[str, object]:
    requested_env = env_code.upper() if env_code else default_submission_env()
    portal_profile = fallback_profile(value)
    if portal_profile:
        profile = load_portal_profile(value)
        profile["preferredEnvCode"] = requested_env
        return profile

    code = client_code_param(value)
    client = query_one(
        """
        SELECT ClientCode, ClientName, SchemaName, DefaultRoute, IsAgent, ActAsSysId, IsActive, Notes
        FROM CFG.Clients
        WHERE ClientCode = ?
        """,
        [code],
    )
    if not client:
        raise HTTPException(status_code=404, detail=f"No CFG.Clients row found for {code}.")
    credential = query_one(
        """
        SELECT TOP 1 ClientCode, EnvCode
        FROM CFG.TSS_Credential
        WHERE ClientCode = ?
        ORDER BY IsActive DESC, CASE WHEN EnvCode = ? THEN 0 ELSE 1 END, EnvCode
        """,
        [code, requested_env],
    ) or {}
    return {
        "portalClientCode": client["ClientCode"],
        "clientCode": client["ClientCode"],
        "clientName": client.get("ClientName") or client["ClientCode"],
        "schemaName": client.get("SchemaName"),
        "defaultRoute": client.get("DefaultRoute") or "A",
        "isAgent": bool(client.get("IsAgent")),
        "actAsSysId": client.get("ActAsSysId"),
        "isActive": bool(client.get("IsActive")),
        "notes": client.get("Notes"),
        "tssCredentialClientCode": credential.get("ClientCode") or client["ClientCode"],
        "preferredEnvCode": requested_env or credential.get("EnvCode") or "TST",
        "requiresEnsBeforeSubmit": True,
        "fileSelection": {},
    }

def tss_operation_payload(
    *,
    client_code: str,
    endpoint: str,
    method: str,
    op_type: str | None,
    route_code: str | None,
    env_code: str | None,
    query: dict[str, object] | None = None,
    payload: dict[str, object] | None = None,
    send: bool = False,
    confirm_live: bool = False,
    manual_step: dict[str, object] | None = None,
) -> dict[str, object]:
    profile = load_tss_client_profile(client_code, env_code=env_code)
    try:
        step = resolve_tss_step(
            str(profile["clientCode"]),
            endpoint,
            method,
            op_type=op_type,
            route_code=route_code or str(profile.get("defaultRoute") or "A"),
        )
    except HTTPException:
        if manual_step is None:
            raise
        step = manual_step
    credential = credential_status(profile, env_code=env_code, include_secret=send and confirm_live)
    if not credential:
        raise HTTPException(status_code=404, detail="No TSS credential row found for this portal profile/environment.")

    base_path = tss_api_base_path()
    destination_url = join_tss_url(credential.get("baseUrl"), base_path, endpoint, query)
    request_payload = dict(payload or {})
    if method.upper() == "POST" and op_type and "op_type" not in request_payload:
        request_payload["op_type"] = op_type

    response: dict[str, object] = {
        "dryRun": not send,
        "sendRequested": send,
        "confirmLive": confirm_live,
        "profile": profile,
        "credential": public_credential_payload(credential),
        "routeStep": step,
        "target": {
            "baseUrl": credential.get("baseUrl"),
            "apiBasePath": base_path,
            "endpoint": normalize_tss_endpoint(endpoint),
            "url": destination_url,
        },
        "request": {
            "method": method.upper(),
            "query": query or {},
            "payload": request_payload if method.upper() != "GET" else None,
        },
        "mappingSource": "CFG.API_Process_Map + CFG.TSS_Environment + CFG.Application_Parameters.SUBMISSION_API_BASE_PATH",
    }
    if not send:
        response["execution"] = "preview_only"
        return response
    if not confirm_live:
        raise HTTPException(status_code=409, detail="Live TSS relay requires confirm_live=true. Run preview first and review target/payload.")
    if not credential.get("isActive"):
        raise HTTPException(status_code=409, detail="TSS credential is not active for this portal profile/environment.")
    if not credential.get("hasPassword") or not credential.get("password"):
        raise HTTPException(status_code=409, detail="TSS credential has no password configured in CFG.TSS_Credential.")

    result = call_tss_http(
        method=method,
        url=destination_url,
        username=str(credential["tssUsername"]),
        password=str(credential["password"]),
        payload=request_payload if method.upper() != "GET" else None,
    )
    response["execution"] = "sent_to_tss"
    response["result"] = result
    return response


def call_tss_http(method: str, url: str, username: str, password: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    body = None
    headers = {"Accept": "application/json"}
    if method.upper() != "GET":
        body = json.dumps(payload or {}, ensure_ascii=False, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, data=body, method=method.upper(), headers=headers)
    started = datetime.now(timezone.utc)
    status_code: int | None = None
    response_text = ""
    ok = False
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - target comes from trusted CFG.
            status_code = int(response.status)
            response_text = response.read(1_000_000).decode("utf-8", errors="replace")
            ok = 200 <= status_code < 300
    except urllib.error.HTTPError as error:
        status_code = int(error.code)
        response_text = error.read(1_000_000).decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        response_text = str(error.reason)
    duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return {
        "statusCode": status_code,
        "ok": ok,
        "durationMs": duration_ms,
        "responseText": response_text,
    }
def session_payload(profile: dict[str, object], username: str) -> dict[str, object]:
    return {
        "tenantCode": profile["portalClientCode"],
        "tenantName": profile["clientName"],
        "username": username,
        "role": "CentralAdmin",
    }


def env_app_login_matches(username: str, password: str) -> bool:
    expected_user = config_value("FLOW_V1_USER")
    expected_password = config_value("FLOW_V1_PASSWORD")
    return bool(
        expected_user
        and expected_password
        and hmac.compare_digest(username, expected_user)
        and hmac.compare_digest(password, expected_password)
    )


@app.get("/api/health")
def health(check_db: bool = Query(False)) -> dict[str, object]:
    payload: dict[str, object] = {"status": "ok", "service": "fusion_portal_api"}
    if not check_db:
        payload["db_checked"] = False
        return payload
    try:
        execute_scalar("SELECT 1")
    except DbUnavailable as exc:
        payload.update({"status": "degraded", "db_checked": True, "db_available": False, "detail": str(exc)})
        return payload
    payload.update({"db_checked": True, "db_available": True})
    return payload


@app.post("/api/auth/login")
def auth_login(request: Request, payload: Annotated[dict[str, object], Body(...)]) -> dict[str, object]:
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise HTTPException(status_code=422, detail="Username and password are required.")

    # Ported from V2 dev02 (11bc217): every attempt is audited, and the audit is
    # best-effort so it can never be the reason a login fails. The correlation id
    # ties the success row to the logout row for the same session.
    correlation_id = uuid4().hex

    # Synovia is the DB-independent demo/admin login. Check it before querying
    # CFG so operators can still enter the read-only demo when SQL is unavailable
    # or Render has not yet been given DB_CONN_STR.
    if env_app_login_matches(username, password):
        default_profile = fallback_profile("BKD")
        if not default_profile:
            raise HTTPException(status_code=503, detail="The Synovia demo profile is not configured.")
        record_login_audit(
            request,
            event_type=EVENT_LOGIN_SUCCESS,
            success=True,
            username=username,
            client_code="BKD",
            correlation_id=correlation_id,
        )
        return {
            "authenticated": True,
            "source": "FLOW_V1_USER",
            "session": {
                "tenantCode": "SYNOVIA",
                "tenantName": "Synovia",
                "username": username,
                "role": "CentralAdmin",
                "mode": "DEMO_ADMIN",
                "authCorrelationId": correlation_id,
            },
            "connection": {
                "portalClientCode": default_profile["portalClientCode"],
                "clientCode": default_profile["clientCode"],
                "clientName": default_profile["clientName"],
                "tssCredentialClientCode": default_profile["tssCredentialClientCode"],
                "preferredEnvCode": default_profile["preferredEnvCode"],
                "requiresEnsBeforeSubmit": default_profile["requiresEnsBeforeSubmit"],
                "fileSelection": default_profile["fileSelection"],
                "credential": None,
                "route": fallback_route(default_profile),
            },
            "defaultClientCode": "BKD",
            "demoMode": True,
            "databaseWrite": False,
            "tssWrite": False,
        }

    try:
        row = query_one(
            """
            SELECT TOP 1 ClientCode, EnvCode, TssUsername, TssPassword, CAST(IsActive AS int) AS IsActive
            FROM CFG.TSS_Credential
            WHERE UPPER(TssUsername) = UPPER(?)
              AND IsActive = 1
              AND ClientCode IN ('BKD', 'PLE', 'CWF')
            ORDER BY CASE
                WHEN ClientCode = 'BKD' AND EnvCode IN ('TST', 'QAS') THEN 0
                WHEN ClientCode = 'BKD' THEN 1
                WHEN ClientCode = 'PLE' AND EnvCode = 'PRD' THEN 2
                WHEN ClientCode = 'CWF' AND EnvCode = 'TST' THEN 2
                ELSE 3
            END, EnvCode
            """,
            [username],
        )
        if row and row.get("TssPassword") and hmac.compare_digest(password, str(row["TssPassword"])):
            portal_code = portal_code_for_tss_client(str(row["ClientCode"]))
            if not portal_code:
                raise HTTPException(status_code=403, detail="This TSS credential is not mapped to a portal client.")
            profile = load_portal_profile(portal_code)
            profile["preferredEnvCode"] = row["EnvCode"]
            session = session_payload(profile, username)
            audit_id = record_login_audit(
                request,
                event_type=EVENT_LOGIN_SUCCESS,
                success=True,
                username=username,
                client_code=str(row["ClientCode"]),
                env_code=str(row["EnvCode"]),
                correlation_id=correlation_id,
            )
            session["authCorrelationId"] = correlation_id
            if audit_id:
                session["loginAuditId"] = audit_id
            return {
                "authenticated": True,
                "source": "CFG.TSS_Credential",
                "session": session,
                "connection": public_connection_payload(profile, env_code=str(row["EnvCode"])),
            }

    except DbUnavailable as exc:
        raise db_error(exc) from exc

    record_login_audit(
        request,
        event_type=EVENT_LOGIN_FAILURE,
        success=False,
        username=username,
        failure_reason="invalid_credentials",
        correlation_id=correlation_id,
    )
    raise HTTPException(status_code=401, detail="Invalid username or password.")


@app.post("/api/auth/logout")
def auth_logout(request: Request, payload: Annotated[dict[str, object] | None, Body()] = None) -> dict[str, object]:
    """Close the audit trail for a session.

    The V3 portal holds its session client-side, so there is nothing server-side
    to clear; this exists so a logout leaves the same trace it does in V2, paired
    to the login row by correlation id.
    """
    body = payload or {}
    audit_id = record_login_audit(
        request,
        event_type=EVENT_LOGOUT,
        success=True,
        username=str(body.get("username") or ""),
        client_code=str(body.get("clientCode") or body.get("client_code") or "") or None,
        env_code=str(body.get("envCode") or body.get("env_code") or "") or None,
        correlation_id=str(body.get("authCorrelationId") or body.get("auth_correlation_id") or "") or None,
    )
    return {"loggedOut": True, "loginAuditId": audit_id, "audited": audit_id is not None}


@app.get("/api/portal/profiles")
def portal_profiles() -> dict[str, object]:
    try:
        return {
            "profiles": [load_portal_profile(str(profile["portalClientCode"])) for profile in fallback_profiles()],
            "source": "CFG.Clients + CFG.TSS_Credential",
        }
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/file-profiles")
def file_profiles(client_code: str | None = Query(None)) -> dict[str, object]:
    profiles = [load_portal_profile(client_code)] if client_code else (portal_profiles()["profiles"])
    return {
        "profiles": [
            {
                "portalClientCode": profile["portalClientCode"],
                "clientCode": profile["clientCode"],
                "clientName": profile["clientName"],
                "fileSelection": profile["fileSelection"],
                "source": "portal_bridge",
            }
            for profile in profiles
        ]
    }


@app.get("/api/admin/settings")
def admin_settings(client_code: str = Query("PLE")) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        return admin_settings_payload(profile)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/admin/validation-diagnostics")
def admin_validation_diagnostics(client_code: str = Query("PLE")) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        _product_master, product_master_context = load_preview_product_master(profile)
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "clientName": profile["clientName"],
        "writeMode": "read_only_diagnostics",
        "databaseWrite": False,
        "tssWrite": False,
        "productMaster": product_master_context,
        "packageTypeResolutionOrder": PACKAGE_TYPE_RESOLUTION_ORDER,

        "notes": [
            "Diagnostics are used by the Portal to explain validation/enrichment behaviour before any DB or TSS write.",
            "PackageType is not a TSS choice-values download in V3; it must come from source data or CFG masterdata and then be normalised.",
        ],
    }


@app.post("/api/admin/settings")
def admin_settings_save(payload: Annotated[dict[str, object], Body(...)]) -> dict[str, object]:
    client_code = str(payload.get("clientCode") or payload.get("client_code") or "PLE")
    try:
        profile = load_portal_profile(client_code)
        return save_admin_settings_payload(profile, payload.get("updates") or [])
    except DbUnavailable as exc:
        raise db_error(exc) from exc

@app.get("/api/tss/connections")
def tss_connections(client_code: str | None = Query(None), env_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profiles = [load_portal_profile(client_code)] if client_code else portal_profiles()["profiles"]
        connections = []
        for profile in profiles:
            credential = credential_status(profile, env_code=env_code)
            connections.append({
                "portalClientCode": profile["portalClientCode"],
                "clientCode": profile["clientCode"],
                "clientName": profile["clientName"],
                "tssCredentialClientCode": profile["tssCredentialClientCode"],
                "preferredEnvCode": profile["preferredEnvCode"],
                "requiresEnsBeforeSubmit": profile["requiresEnsBeforeSubmit"],
                "fileSelection": profile["fileSelection"],
                "credential": credential,
                "route": load_submission_route(profile),
            })
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    return {"connections": connections}


def tss_readiness_payload(profile: dict[str, object]) -> dict[str, object]:
    connection = public_connection_payload(profile)
    route = connection["route"]
    candidate = query_one(
        """
        SELECT TOP 1 c.ConsignmentRowID,
               COALESCE(c.declaration_number, h.declaration_number) AS DeclarationNumber,
               g.GoodsCount
        FROM PRS.Consignment c
        LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
        OUTER APPLY (
            SELECT COUNT(*) AS GoodsCount
            FROM PRS.Goods_Item gi
            WHERE gi.ConsignmentRowID = c.ConsignmentRowID
        ) g
        WHERE c.ClientCode = ?
        ORDER BY
            CASE WHEN COALESCE(c.declaration_number, h.declaration_number) IS NOT NULL THEN 0 ELSE 1 END,
            CASE WHEN g.GoodsCount > 0 THEN 0 ELSE 1 END,
            c.ConsignmentRowID
        """,
        [profile["clientCode"]],
    )
    if not candidate:
        return {
            "portalClientCode": profile["portalClientCode"],
            "clientCode": profile["clientCode"],
            "connection": connection,
            "ready": False,
            "dataReady": False,
            "candidate": None,
            "plan": None,
            "blockers": [f"No PRS.Consignment rows for data client {profile['clientCode']}"],
            "invariant": "UPDATE_CONSIGNMENT_WITH_ENS must run before SUBMIT_CONSIGNMENT.",
        }

    consignment, goods = load_consignment_submission_data(int(candidate["ConsignmentRowID"]), profile)
    plan = build_consignment_submission_plan(profile=profile, consignment=consignment, goods_items=goods, route=route)
    blockers = list(plan.get("routeBlockers") or [])
    if plan.get("missing"):
        blockers.append("Missing required TSS fields: " + ", ".join(plan["missing"]))
    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "connection": connection,
        "ready": bool(plan["ready"]),
        "dataReady": True,
        "candidate": {
            "consignmentRowId": candidate["ConsignmentRowID"],
            "hasEnsDeclarationNumber": bool(plan["ensDeclarationNumber"]),
            "goodsItemCount": plan["goodsItemCount"],
        },
        "plan": plan,
        "blockers": blockers,
        "invariant": "UPDATE_CONSIGNMENT_WITH_ENS must run before SUBMIT_CONSIGNMENT.",
    }

@app.get("/api/tss/readiness")
def tss_readiness(client_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profiles = [load_portal_profile(client_code)] if client_code else portal_profiles()["profiles"]
        return {"readiness": [tss_readiness_payload(profile) for profile in profiles]}
    except DbUnavailable as exc:
        raise db_error(exc) from exc

@app.get("/api/tss/route-plan")
def tss_route_plan(client_code: str = Query("PLE"), env_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profile = load_tss_client_profile(client_code, env_code=env_code)
        credential = credential_status(profile)
        return {
            "profile": profile,
            "credential": credential,
            "route": load_submission_route(profile),
            "invariant": "UPDATE_CONSIGNMENT_WITH_ENS must complete before SUBMIT_CONSIGNMENT.",
        }
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/connections/test")
def test_tss_connection(client_code: str = Query("PLE"), env_code: str | None = Query(None)) -> dict[str, object]:
    try:
        profile = load_tss_client_profile(client_code, env_code=env_code)
        credential = credential_status(profile, env_code=env_code, include_secret=True)
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not credential:
        raise HTTPException(status_code=404, detail="No TSS credential row found for this portal profile/environment.")
    if not credential.get("hasPassword") or not credential.get("password"):
        raise HTTPException(status_code=409, detail="TSS credential has no password configured in CFG.TSS_Credential.")
    if not credential.get("baseUrl"):
        raise HTTPException(status_code=409, detail="TSS environment has no BaseUrl configured.")

    api_base_path = tss_choice_values_base_path()
    test_endpoint = "/country"
    url = join_tss_url(credential["baseUrl"], api_base_path, test_endpoint)
    token = base64.b64encode(f"{credential['tssUsername']}:{credential['password']}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}", "Accept": "application/json"})
    http_status: int | None = None
    detail = ""
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - URL comes from trusted CFG.TSS_Environment
            http_status = int(response.status)
            detail = response.read(160).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        http_status = int(error.code)
        detail = error.read(160).decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        detail = str(error.reason)[:160]

    result = classify_tss_status(http_status)
    severity, message = describe_tss_test_result(result, http_status)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute(
            """
            UPDATE CFG.TSS_Credential
            SET LastVerified = SYSUTCDATETIME(), LastStatus = ?, HttpStatus = ?, UpdatedAt = SYSUTCDATETIME()
            WHERE ClientCode = ? AND EnvCode = ?
            """,
            [result, http_status, credential["credentialClientCode"], credential["envCode"]],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "credentialClientCode": credential["credentialClientCode"],
        "envCode": credential["envCode"],
        "httpStatus": http_status,
        "result": result,
        "ok": result == "PASS",
        "reachable": http_status is not None,
        "severity": severity,
        "message": message,
        "apiBasePath": api_base_path,
        "endpoint": test_endpoint,
        "detail": detail,
        "checkedAt": checked_at,
    }


@app.post("/api/tss/consignments/{consignment_row_id}/update-ens-plan")
def update_ens_plan(consignment_row_id: int, client_code: str = Query("PLE")) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        row = query_one(
            """
            SELECT c.ConsignmentRowID, c.EnsHeaderRowID, c.ClientCode, c.consignment_number,
                   c.declaration_number AS ConsignmentDeclarationNumber,
                   h.declaration_number AS HeaderDeclarationNumber,
                   h.MovementKey, h.arrival_date_time
            FROM PRS.Consignment c
            LEFT JOIN PRS.ENS_Header h ON h.EnsHeaderRowID = c.EnsHeaderRowID
            WHERE c.ConsignmentRowID = ? AND c.ClientCode = ?
            """,
            [consignment_row_id, profile["clientCode"]],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not row:
        raise HTTPException(status_code=404, detail="Consignment was not found for this portal/client profile.")

    ens_value = row.get("ConsignmentDeclarationNumber") or row.get("HeaderDeclarationNumber")
    return {
        "profile": profile,
        "consignment": row,
        "hasEnsDeclarationNumber": bool(ens_value),
        "ensDeclarationNumber": ens_value,
        "route": load_submission_route(profile),
        "submitAllowed": bool(ens_value),
        "invariant": "Submit is blocked until UPDATE_CONSIGNMENT_WITH_ENS has an ENS/declaration_number value.",
    }

@app.post("/api/tss/consignments/{consignment_row_id}/submit")
def submit_consignment_to_tss(
    consignment_row_id: int,
    client_code: str = Query("PLE"),
    dry_run: bool = Query(True),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        profile = load_portal_profile(client_code)
        credential = credential_status(profile, include_secret=not dry_run)
        route = load_submission_route(profile)
        consignment, goods = load_consignment_submission_data(consignment_row_id, profile)
        plan = build_consignment_submission_plan(profile=profile, consignment=consignment, goods_items=goods, route=route)
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    response: dict[str, object] = {
        "dryRun": dry_run,
        "profile": profile,
        "credential": public_credential_payload(credential),
        "consignmentRowId": consignment_row_id,
        "plan": plan,
    }
    if dry_run:
        response["execution"] = "preview_only"
        return response

    if not confirm_live:
        raise HTTPException(status_code=409, detail="Live TSS submit requires confirm_live=true. Re-run dry_run first and review payloads.")
    if not plan["ready"]:
        raise HTTPException(status_code=409, detail={"message": "Consignment is not ready for TSS submit.", "missing": plan["missing"]})
    if not credential:
        raise HTTPException(status_code=404, detail="No TSS credential row found for this portal profile/environment.")
    if not credential.get("isActive"):
        raise HTTPException(status_code=409, detail="TSS credential is not active for this portal profile/environment.")
    if not credential.get("hasPassword") or not credential.get("password"):
        raise HTTPException(status_code=409, detail="TSS credential has no password configured in CFG.TSS_Credential.")
    if not credential.get("baseUrl"):
        raise HTTPException(status_code=409, detail="TSS environment has no BaseUrl configured.")

    results = []
    for step in plan["steps"]:
        result = post_tss_json(
            base_url=str(credential["baseUrl"]),
            username=str(credential["tssUsername"]),
            password=str(credential["password"]),
            endpoint=str(step["endpoint"]),
            payload=step["payload"],
        )
        try:
            log_api_trace(str(profile["clientCode"]), step, step["payload"], result)
        except DbUnavailable as exc:
            raise db_error(exc) from exc
        results.append({
            "operationCode": step["operationCode"],
            "endpoint": step["endpoint"],
            "statusCode": result["statusCode"],
            "ok": result["ok"],
            "durationMs": result["durationMs"],
            "responseText": result["responseText"],
        })
        if not result["ok"]:
            break

    response["execution"] = "sent_to_tss"
    response["results"] = results
    response["allOk"] = all(item["ok"] for item in results) and len(results) == len(plan["steps"])
    return response


@app.get("/api/tss/api-map")
def tss_api_map(client_code: str = Query("BKD"), route_code: str | None = Query(None)) -> dict[str, object]:
    try:
        route = load_tss_api_map(client_code, route_code=route_code)
        return {
            "clientCode": client_code_param(client_code),
            "routeCode": route_code,
            "steps": route,
            "coveredPublicEndpoints": sorted({str(step["publicEndpoint"]) for step in route}),
            "source": "CFG.API_Process_Map",
        }
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/api-map/{step_no}")
def tss_api_map_step(step_no: int, client_code: str = Query("BKD"), route_code: str | None = Query("A")) -> dict[str, object]:
    try:
        matches = [step for step in load_tss_api_map(client_code, route_code=route_code) if int(step.get("StepNo") or -1) == step_no]
        if not matches:
            raise HTTPException(status_code=404, detail=f"No active route step {step_no} for {client_code} route {route_code}.")
        return {"step": matches[0], "source": "CFG.API_Process_Map"}
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/permission-grant")
def tss_permission_grant(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    importer_eori: str | None = Query(None),
    eori: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    query = {"importer_eori": importer_eori or eori}
    try:
        return tss_operation_payload(
            client_code=client_code,
            endpoint="/permission_grant",
            method="GET",
            op_type="read",
            route_code="A",
            env_code=env_code,
            query=query,
            send=send,
            confirm_live=confirm_live,
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/headers")
def tss_headers_create(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(
            client_code=client_code,
            endpoint="/headers",
            method="POST",
            op_type="create",
            route_code="A",
            env_code=env_code,
            payload=payload,
            send=send,
            confirm_live=confirm_live,
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/headers")
def tss_headers_read(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    reference: str | None = Query(None),
    declaration_number: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    query = {"reference": reference or declaration_number}
    try:
        return tss_operation_payload(
            client_code=client_code,
            endpoint="/headers",
            method="GET",
            op_type="read",
            route_code="A",
            env_code=env_code,
            query=query,
            send=send,
            confirm_live=confirm_live,
            manual_step={
                "operationCode": "DECLARATION_HEADER_READ",
                "resourceName": "Declaration Header",
                "endpoint": "/headers",
                "httpMethod": "GET",
                "opType": "read",
                "source": "Modules.Submission.mirror_ens",
                "notes": "Mirror reads a submitted ENS header with /headers?reference=ENS...; route A maps create in CFG.API_Process_Map.",
            },
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/consignments")
def tss_consignments_create(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/consignments", method="POST", op_type="create", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/consignments/submit")
def tss_consignments_submit(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/consignments", method="POST", op_type="submit", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/goods")
def tss_goods_create(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/goods", method="POST", op_type="create", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/goods")
def tss_goods_lookup(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    goods_id: str | None = Query(None),
    consignment_number: str | None = Query(None),
    declaration_number: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    query = {"goods_id": goods_id, "consignment_number": consignment_number, "declaration_number": declaration_number}
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/goods", method="GET", op_type="lookup", route_code="A", env_code=env_code, query=query, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/goods/update")
def tss_goods_update(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/goods", method="POST", op_type="update", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/simplified-frontier-declarations")
def tss_sfd_lookup(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    consignment_number: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/simplified_frontier_declarations", method="GET", op_type="lookup", route_code="A", env_code=env_code, query={"consignment_number": consignment_number}, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/gvms-gmr")
def tss_gvms_gmr_create(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/gvms_gmr", method="POST", op_type="create", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/gvms-gmr/submit")
def tss_gvms_gmr_submit(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/gvms_gmr", method="POST", op_type="submit", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/gvms-gmr")
def tss_gvms_gmr_read(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    gmr_id: str | None = Query(None),
    reference: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/gvms_gmr", method="GET", op_type="read", route_code="A", env_code=env_code, query={"gmr_id": gmr_id or reference}, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/tss/supplementary-declarations")
def tss_supplementary_declarations_lookup(
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    sfd_number: str | None = Query(None),
    reference: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/supplementary_declarations", method="GET", op_type="lookup", route_code="A", env_code=env_code, query={"sfd_number": sfd_number or reference}, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.post("/api/tss/supplementary-declarations/submit")
def tss_supplementary_declarations_submit(
    payload: Annotated[dict[str, object] | None, Body()] = None,
    client_code: str = Query("BKD"),
    env_code: str | None = Query(None),
    send: bool = Query(False),
    confirm_live: bool = Query(False),
) -> dict[str, object]:
    try:
        return tss_operation_payload(client_code=client_code, endpoint="/supplementary_declarations", method="POST", op_type="submit", route_code="A", env_code=env_code, payload=payload, send=send, confirm_live=confirm_live)
    except DbUnavailable as exc:
        raise db_error(exc) from exc


@app.get("/api/session")
def session(client_code: str = Query("PLE")) -> dict[str, object]:
    code = client_code_param(client_code)
    try:
        client = query_one(
            """
            SELECT ClientCode, ClientName, SchemaName, DefaultRoute, IsAgent, IsActive
            FROM CFG.Clients
            WHERE ClientCode = ?
            """,
            [code],
        )
        paths = query_all(
            """
            SELECT PathType, PathValue, IsActive
            FROM CFG.Folder_Paths
            WHERE ClientCode = ? AND IsActive = 1
            ORDER BY PathType
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not client:
        raise HTTPException(status_code=404, detail=f"Client {code} was not found in CFG.Clients.")

    return {
        "username": "synovia",
        "role": "CentralAdmin",
        "tenantCode": client["ClientCode"],
        "tenantName": client.get("ClientName") or client["ClientCode"],
        "schemaName": client.get("SchemaName"),
        "defaultRoute": client.get("DefaultRoute"),
        "isActive": bool(client.get("IsActive")),
        "folderPaths": paths,
    }


@app.get("/api/dashboard")
def dashboard(client_code: str = Query("PLE")) -> dict[str, object]:
    """Tenant totals as TSS holds them, plus the local ingestion side.

    The counts the operator reads on the dashboard have to agree with the
    consignment list, so the declaration/consignment/goods figures come from the
    same mirror the list does. The ingestion figures are a different question -
    "what did Fusion pull in" - and stay on the Release 1 database, which is the
    only place that knows. They are reported as two separate blocks rather than
    added together, because summing them would be meaningless.
    """
    code = client_code_param(client_code)
    try:
        schema = tenant_schema(client_code)
    except UnknownTenant as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counts: dict[str, object] = {}
    mirror_available = True
    try:
        sfd_counts = (
            f"""
                (SELECT COUNT(*) FROM [{schema}].[SFD_Declarations]) AS SfdDeclarations,
                (SELECT COUNT(*) FROM [{schema}].[SDI_Declarations]) AS SdiDeclarations,
            """
            if tenant_has_sfd(schema)
            else """
                CAST(NULL AS int) AS SfdDeclarations,
                CAST(NULL AS int) AS SdiDeclarations,
            """
        )
        mirror_counts = mirror_query_one(
            f"""
            SELECT
                (SELECT COUNT(*) FROM [{schema}].[ENS_Headers]) AS EnsHeaders,
                (SELECT COUNT(*) FROM [{schema}].[Consignments]) AS Consignments,
                (SELECT COUNT(*) FROM [{schema}].[GoodsItems]) AS GoodsItems,
                {sfd_counts}
                (SELECT COUNT(*) FROM [{schema}].[Consignments]
                  WHERE UPPER(LTRIM(RTRIM(COALESCE(status, '')))) = 'ARRIVED') AS ArrivedConsignments,
                (SELECT COUNT(*) FROM [{schema}].[Consignments]
                  WHERE UPPER(LTRIM(RTRIM(COALESCE(status, '')))) = 'CANCELLED') AS CancelledConsignments
            """
        )
        counts.update(mirror_counts or {})
    except DbUnavailable:
        # The mirror being down must not blank the whole dashboard; say so instead.
        mirror_available = False

    try:
        local = query_one(
            """
            SELECT
                (SELECT COUNT(*) FROM ING.Inbound_File WHERE ClientCode = ?) AS InboundFiles,
                (SELECT COUNT(*) FROM ING.Raw_Record WHERE ClientCode = ?) AS RawRecords,
                (SELECT COUNT(*) FROM ING.Source_Email WHERE ClientCode = ?) AS SourceEmails
            """,
            [code, code, code],
        )
        counts.update(local or {})
        latest = query_all(
            """
            SELECT TOP 10 FileID, SourceChannel, SourceName, RowsLanded, Status, CreatedAt
            FROM ING.Inbound_File
            WHERE ClientCode = ?
            ORDER BY CreatedAt DESC, FileID DESC
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    return {
        "clientCode": code,
        "tenantSchema": schema,
        "counts": counts,
        "latestInboundFiles": latest,
        "sources": {
            "declarations": f"Fusion_Flow_V3 [{schema}]" if mirror_available else "unavailable",
            "ingestion": "Fusion_Flow_V3_QAS ING.*",
        },
        "mirrorAvailable": mirror_available,
    }


CONTROL_TOWER_OBJECTS = [
    "CFG.Job",
    "CFG.Application_Parameters",
    "ING.Inbound_File",
    "ING.Raw_Record",
    "ING.Source_Email",
    "ING.BKD_Raw_ENS",
    "ING.BKD_Raw_Sales_Orders",
    "PRS.ENS_Header",
    "PRS.Consignment",
    "PRS.Goods_Item",
    "PRS.BKD_ENS_Header_Tracking",
    "STG.BKD_ENS_Header",
    "API.Call",
    "EXC.Execution",
    "EXC.Job_Queue",
    "LOG.Process_Log",
    "LOG.Notification",
]


@app.get("/api/control-tower")
def control_tower(client_code: str = Query("PLE"), limit: int = Query(20, ge=1, le=100)) -> dict[str, object]:
    code = client_code_param(client_code)
    top = safe_limit(limit, default=20, maximum=100)
    try:
        availability = {name: object_exists(name) for name in CONTROL_TOWER_OBJECTS}
        counts = {
            "inboundFiles": safe_count("ING.Inbound_File", " WHERE ClientCode = ?", [code]),
            "rawRecords": safe_count("ING.Raw_Record", " WHERE ClientCode = ?", [code]),
            "sourceEmails": safe_count("ING.Source_Email", " WHERE ClientCode = ?", [code]),
            "bkdRawEns": safe_count("ING.BKD_Raw_ENS"),
            "bkdRawSalesOrders": safe_count("ING.BKD_Raw_Sales_Orders"),
            "prsHeaders": safe_count("PRS.ENS_Header", " WHERE ClientCode = ?", [code]),
            "prsConsignments": safe_count("PRS.Consignment", " WHERE ClientCode = ?", [code]),
            "prsGoodsItems": safe_count("PRS.Goods_Item", " WHERE ClientCode = ?", [code]),
            "prsTrackedMovements": safe_count("PRS.BKD_ENS_Header_Tracking", " WHERE ClientCode = ?", [code]),
            "stgHeaders": safe_count("STG.BKD_ENS_Header", " WHERE ClientCode = ?", [code]),
            "stgReady": safe_count("STG.BKD_ENS_Header", " WHERE ClientCode = ? AND Fusion_Status IN ('STG_MATERIALISED','READY')", [code]),
            "stgSubmitted": safe_count("STG.BKD_ENS_Header", " WHERE ClientCode = ? AND Fusion_Status IN ('SUBMITTED','RECONCILED')", [code]),
            "apiCalls": safe_count("API.Call", " WHERE ClientCode = ? OR ClientCode IS NULL", [code]),
            "apiErrors": safe_count("API.Call", " WHERE (ClientCode = ? OR ClientCode IS NULL) AND Success = 0 AND IsDryRun = 0", [code]),
            "activeJobs": safe_count("CFG.Job", " WHERE (ClientCode = ? OR ClientCode IS NULL) AND IsActive = 1", [code]),
            "queuePending": safe_count("EXC.Job_Queue", " WHERE Status = 'PENDING'"),
            "notifications": safe_count("LOG.Notification", " WHERE ClientCode = ?", [code]),
            "notificationFailures": safe_count("LOG.Notification", " WHERE ClientCode = ? AND Status IN ('FAILED','ERROR')", [code]),
        }

        jobs = safe_rows(
            "CFG.Job",
            f"""
            SELECT TOP {top}
                JobCode, JobName, ModuleName, ClientCode, JobType, StepNo, EntryPoint,
                InputSource, OutputTarget, Schedule, IsActive
            FROM CFG.Job
            WHERE ClientCode = ? OR ClientCode IS NULL
            ORDER BY ModuleName, COALESCE(StepNo, 999), JobCode
            """,
            [code],
        )
        queue = safe_rows(
            "EXC.Job_Queue",
            f"""
            SELECT TOP {top}
                QueueID, Verb, MovementKey, ClientCode, Status, Attempts, RequestedBy,
                RequestedAt, StartedAt, FinishedAt, ExitCode, ResultMessage
            FROM EXC.Job_Queue
            WHERE ClientCode = ? OR ClientCode IS NULL
            ORDER BY CASE Status WHEN 'PENDING' THEN 0 WHEN 'RUNNING' THEN 1 WHEN 'FAILED' THEN 2 ELSE 3 END,
                     QueueID DESC
            """,
            [code],
        )
        executions = safe_rows(
            "EXC.Execution",
            f"""
            SELECT TOP {top}
                ExecutionID, EnvCode, ClientCode, ModuleName, ProcessName, RunMode, Status,
                ItemsFound, ItemsProcessed, ItemsFailed, StartedAt, EndedAt, ErrorMessage
            FROM EXC.Execution
            WHERE ClientCode = ? OR ClientCode IS NULL
            ORDER BY StartedAt DESC, ExecutionID DESC
            """,
            [code],
        )
        activity = safe_rows(
            "LOG.Process_Log",
            f"""
            SELECT TOP {top}
                LogID, ExecutionID, ClientCode, ModuleName, StepName, LogLevel, Message, CreatedAt
            FROM LOG.Process_Log
            WHERE ClientCode = ? OR ClientCode IS NULL
            ORDER BY CreatedAt DESC, LogID DESC
            """,
            [code],
        )
        api_calls = safe_rows(
            "API.Call",
            f"""
            SELECT TOP {top}
                CallID, CreatedAt, ClientCode, MovementKey, Declaration_Number, ModuleName,
                ProcessName, ResourceName, OpType, EnvCode, HttpMethod, StatusCode,
                Success, IsDryRun, DurationMs, ErrorMessage
            FROM API.Call
            WHERE ClientCode = ? OR ClientCode IS NULL
            ORDER BY CreatedAt DESC, CallID DESC
            """,
            [code],
        )
        notifications = safe_rows(
            "LOG.Notification",
            f"""
            SELECT TOP {top}
                NotificationID, EventType, EntityKind, EntityId, Channel, Provider,
                ToRecipients, Subject, Status, HttpStatus, ErrorMessage, CreatedAt, SentAt
            FROM LOG.Notification
            WHERE ClientCode = ?
            ORDER BY CreatedAt DESC, NotificationID DESC
            """,
            [code],
        )
        params = safe_rows(
            "CFG.Application_Parameters",
            """
            SELECT ParameterKey, ParameterValue, ValueType, IsActive, UpdatedAt
            FROM CFG.Application_Parameters
            WHERE ParameterKey IN (
                'INGESTION_DRY_RUN', 'PROCESSING_DRY_RUN', 'SUBMISSION_DRY_RUN',
                'SUBMISSION_ENV', 'PROCESSING_MODE', 'PROCESSING_TRANSACTION_MODE',
                'NOTIFY_ENS_RECEIVED_ENABLED', 'NOTIFY_CONSIGNMENTS_RECEIVED_ENABLED',
                'NOTIFY_STAGING_FAILURES_ENABLED', 'NOTIFY_MOVEMENT_AUTHORISED_ENABLED'
            )
            ORDER BY ParameterKey
            """,
        )
        movements = safe_rows(
            "PRS.BKD_ENS_Header_Tracking",
            f"""
            SELECT TOP {top}
                MovementKey, ClientCode, Fusion_Status, Tss_Status, Declaration_Number,
                SourceChannel, SourceFile, LastExecutionID, CreatedAt, UpdatedAt,
                'PRS.BKD_ENS_Header_Tracking' AS SourceTable
            FROM PRS.BKD_ENS_Header_Tracking
            WHERE ClientCode = ?
            ORDER BY UpdatedAt DESC, TrackingID DESC
            """,
            [code],
        )
        if not movements:
            movements = safe_rows(
                "STG.BKD_ENS_Header",
                f"""
                SELECT TOP {top}
                    MovementKey, ClientCode, Fusion_Status, Tss_Status, declaration_number AS Declaration_Number,
                    PromoteExecutionID AS LastExecutionID, CreatedAt, UpdatedAt,
                    'STG.BKD_ENS_Header' AS SourceTable
                FROM STG.BKD_ENS_Header
                WHERE ClientCode = ?
                ORDER BY UpdatedAt DESC, StgID DESC
                """,
                [code],
            )
        if not movements:
            movements = safe_rows(
                "PRS.ENS_Header",
                f"""
                SELECT TOP {top}
                    MovementKey, ClientCode, Status AS Fusion_Status, declaration_number AS Declaration_Number,
                    ExecutionID AS LastExecutionID, CreatedAt, UpdatedAt,
                    'PRS.ENS_Header' AS SourceTable
                FROM PRS.ENS_Header
                WHERE ClientCode = ?
                ORDER BY UpdatedAt DESC, EnsHeaderRowID DESC
                """,
                [code],
            )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    pipeline = [
        {"stage": "Source", "label": "Inbound files", "table": "ING.Inbound_File", "count": counts["inboundFiles"], "tone": "flow"},
        {"stage": "Raw", "label": "Raw rows", "table": "ING.Raw_Record / BKD_Raw_*", "count": counts["rawRecords"] + counts["bkdRawEns"] + counts["bkdRawSalesOrders"], "tone": "sky"},
        {"stage": "Processing", "label": "PRS records", "table": "PRS.ENS_Header / Consignment / Goods_Item", "count": counts["prsHeaders"] + counts["prsConsignments"] + counts["prsGoodsItems"], "tone": "good"},
        {"stage": "Staging", "label": "Submission-ready", "table": "STG.BKD_ENS_Header", "count": counts["stgHeaders"], "tone": "fusion"},
        {"stage": "TSS", "label": "API calls", "table": "API.Call", "count": counts["apiCalls"], "tone": "muted"},
    ]
    summary = [
        {"label": "Tracked movements", "value": counts["prsTrackedMovements"] or counts["prsHeaders"] or counts["stgHeaders"], "source": "PRS/STG", "tone": "flow"},
        {"label": "Active jobs", "value": counts["activeJobs"], "source": "CFG.Job", "tone": "good"},
        {"label": "Queue pending", "value": counts["queuePending"], "source": "EXC.Job_Queue", "tone": "fusion"},
        {"label": "API errors", "value": counts["apiErrors"], "source": "API.Call", "tone": "bad"},
    ]
    return {
        "clientCode": code,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "availability": availability,
        "counts": counts,
        "summary": summary,
        "pipeline": pipeline,
        "jobs": jobs,
        "queue": queue,
        "executions": executions,
        "activity": activity,
        "apiCalls": api_calls,
        "notifications": notifications,
        "params": params,
        "movements": movements,
    }

@app.get("/api/ingestion/files")
def ingestion_files(
    client_code: str = Query("PLE"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, object]:
    code = client_code_param(client_code)
    top = safe_limit(limit, default=50)
    try:
        rows = query_all(
            f"""
            SELECT TOP {top}
                FileID, ExecutionID, TransactionID, ClientCode, SourceChannel, SourceName, SourcePath,
                Mailbox, Sender, ReceivedUtc, SizeBytes, ContentType, RowsLanded, Status, FailReason, CreatedAt
            FROM ING.Inbound_File
            WHERE ClientCode = ?
            ORDER BY CreatedAt DESC, FileID DESC
            """,
            [code],
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc
    return {"clientCode": code, "files": rows}


@app.get("/api/declarations")
def declarations(
    client_code: str = Query("PLE"),
    limit: int = Query(100, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    q: str = Query(""),
    api_date_range: str = Query("all"),
) -> dict[str, object]:
    """ENS headers as TSS holds them, from the tenant mirror.

    declaration_number is the ENS reference and the primary key in every tenant
    schema, so it is both the identity and a unique index to page on. The consignment
    and goods counts are correlated subqueries over indexed columns, which is
    affordable only because the page is cut first - CWF carries 11,445 headers.
    """
    code = client_code_param(client_code)
    try:
        schema = tenant_schema(client_code)
    except UnknownTenant as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    size = safe_limit(page_size if page_size is not None else limit)
    date_range = normalise_tss_api_date_range(api_date_range)
    date_clause, date_params = consignment_date_filter(date_range)

    where = ["1 = 1"]
    params: list[object] = []
    clean_q = q.strip()
    if clean_q:
        like = f"%{clean_q}%"
        where.append(
            """(
                h.declaration_number LIKE ? OR h.arrival_port LIKE ? OR
                h.carrier_name LIKE ? OR h.carrier_eori LIKE ? OR h.conveyance_ref LIKE ?
            )"""
        )
        params.extend([like] * 5)
    if date_clause:
        where.append(date_clause)
        params.extend(date_params)

    try:
        header_id = ens_header_id_expression(schema)
        created_at = ens_header_loaded_at_expression(schema)

        page_refs = [
            row["declaration_number"]
            for row in mirror_query_all(
                f"""
                SELECT h.declaration_number
                FROM [{schema}].[ENS_Headers] h
                WHERE {' AND '.join(where)}
                ORDER BY h.declaration_number DESC
                OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                """,
                params + [max(0, (page - 1) * size), size],
            )
            or []
        ]
        if page_refs:
            page_clause = "\n              AND h.declaration_number IN ({})".format(
                ", ".join("?" for _ in page_refs)
            )
            page_params: list[object] = list(page_refs)
        else:
            page_clause = "\n              AND 1 = 0"
            page_params = []

        rows = mirror_query_all(
            f"""
            SELECT TOP {size}
                {header_id} AS EnsHeaderRowID,
                h.declaration_number AS DeclarationNumber,
                CAST(NULL AS nvarchar(100)) AS MovementKey,
                h.status AS Status,
                h.status AS TssStatus,
                h.movement_type AS MovementType,
                h.arrival_port AS ArrivalPort,
                h.arrival_date_time AS ArrivalDateTime,
                h.arrival_date_time_utc AS ArrivalDateTimeUtc,
                h.carrier_name AS CarrierName,
                h.carrier_eori AS CarrierEori,
                h.haulier_eori AS HaulierEori,
                h.conveyance_ref AS ConveyanceRef,
                h.seal_number AS SealNumber,
                h.route AS Route,
                h.error_message AS ErrorMessage,
                (SELECT COUNT(*) FROM [{schema}].[Consignments] c
                  WHERE c.declaration_number = h.declaration_number) AS Consignments,
                (SELECT COUNT(*) FROM [{schema}].[GoodsItems] g
                  WHERE g.declaration_number = h.declaration_number) AS GoodsItems,
                CAST(NULL AS nvarchar(100)) AS SourceChannel,
                CAST(NULL AS nvarchar(255)) AS SourceFile,
                CAST(NULL AS bigint) AS LastExecutionID,
                {created_at} AS CreatedAt,
                h.updated_at AS UpdatedAt
            FROM [{schema}].[ENS_Headers] h
            WHERE {' AND '.join(where)}{page_clause}
            ORDER BY h.declaration_number DESC
            """,
            params + page_params,
        )

        total = int(
            mirror_execute_scalar(
                f"""
                SELECT COUNT(*)
                FROM [{schema}].[ENS_Headers] h
                WHERE {' AND '.join(where)}
                """,
                params,
            )
            or 0
        )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    source_table = f"{schema}.ENS_Headers"
    for row in rows:
        row["ClientCode"] = code
        row["SourceTable"] = source_table

    total_pages = max(1, (total + size - 1) // size) if total else 1
    return {
        "clientCode": code,
        "tenantSchema": schema,
        "source": f"Fusion_Flow_V3 [{schema}].ENS_Headers",
        "declarations": rows,
        "statusVocabulary": status_vocabulary_rows(),
        "apiDateRange": date_range,
        "apiDateRangeOptions": [{"value": key, "label": label} for key, label in TSS_API_DATE_RANGE_OPTIONS],
        "pagination": {
            "page": min(page, total_pages),
            "pageSize": size,
            "totalPages": total_pages,
            "filteredTotal": total,
            "sqlPaged": True,
        },
    }

# The consignment list hydrates every row it returns: a goods count per row, the
# SFD and SDI lookups, and the ENS header join. Ported from V2 dev02 (3b95601),
# where that shape cost 8,662 ms to render a 20-row page at 23,619 consignments
# and deep pages timed out outright. PLE and CWF hold over 100,000 each.
#
# OFFSET alone does not fix it: SQL Server still evaluates the per-row work for
# every row before the offset and then discards it. So the page's references are
# picked by a cheap query over the tenant's Consignments table, and only those
# rows are hydrated. That is what makes the per-row cost proportional to the page
# rather than to the tenant - measured flat at ~450 ms for page 1 and page 500 of
# PLE's 100,000.
#
# Every filter reaches SQL, including the TSS arrival-date range: ENS_Headers
# carries arrival_date_time_utc as a real datetime2 alongside the dd/MM/yyyy
# string, so the window is a bound comparison rather than a Python scan over a
# capped prefix. That matters - a capped scan ordered by DEC reference would have
# reported "0 arrivals this month" for a tenant that has them.


def consignment_date_filter(api_date_range: str) -> tuple[str, list[object]]:
    """The SQL predicate for a TSS arrival-date range, and its bound parameters.

    Empty when the range is 'all'. The window comes from tss_api_date_bounds so
    the SQL path and the in-Python predicate cannot drift apart. A row with no
    arrival date falls outside any specific range, which is what the in-Python
    version does too: an unknown date is not evidence of a match.
    """
    start, end = tss_api_date_bounds(api_date_range)
    clause_parts: list[str] = []
    params: list[object] = []
    if start is not None:
        clause_parts.append("h.arrival_date_time_utc >= ?")
        params.append(start)
    if end is not None:
        clause_parts.append("h.arrival_date_time_utc < ?")
        params.append(end)
    if not clause_parts:
        return "", []
    return " AND ".join(clause_parts), params


def consignment_reference_expressions(schema: str) -> dict[str, str]:
    """SQL fragments that absorb the differences between tenant schemas.

    The schema name is interpolated rather than bound because it is part of the
    object name, not a value. It comes from tenant_schema()'s allowlist and is
    re-checked against ^[A-Z]{3}$ there; caller input never reaches this string.
    """
    if tenant_has_sfd(schema):
        sfd = (
            f"(SELECT TOP 1 s.sfd_number FROM [{schema}].[SFD_Declarations] s "
            "WHERE s.consignment_number = c.consignment_number)"
        )
        sdi = (
            f"(SELECT TOP 1 d.sup_dec_number FROM [{schema}].[SDI_Declarations] d "
            "WHERE d.consignment_number = c.consignment_number)"
        )
    else:
        # CRS carries no downstream declaration tables at all.
        sfd = "CAST(NULL AS nvarchar(100))"
        sdi = "CAST(NULL AS nvarchar(100))"
    return {
        "consignmentId": consignment_id_expression(schema),
        "loadedAt": consignment_loaded_at_expression(schema),
        "goodsCount": (
            f"(SELECT COUNT(*) FROM [{schema}].[GoodsItems] g "
            "WHERE g.consignment_number = c.consignment_number)"
        ),
        "sfd": sfd,
        "sdi": sdi,
    }


@app.get("/api/consignments")
def consignments(
    client_code: str = Query("PLE"),
    status: str = Query("ALL"),
    q: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1, le=500),
    api_date_range: str = Query("all"),
) -> dict[str, object]:
    code = client_code_param(client_code)
    try:
        schema = tenant_schema(client_code)
    except UnknownTenant as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    size = safe_limit(page_size if page_size is not None else limit)
    date_range = normalise_tss_api_date_range(api_date_range)
    date_clause, date_params = consignment_date_filter(date_range)

    # There is no ClientCode column to filter on: the schema is the tenant. So the
    # only predicates are the caller's own. base_where is split out so the status
    # tab counts can reuse every predicate except the status one - a tab count that
    # honoured its own filter would always equal the page.
    #
    # The arrival-date predicate names h, so any query carrying it needs the header
    # join even when it wants nothing else from the header.
    base_where = ["1 = 1"]
    base_params: list[object] = []
    clean_q = q.strip()
    if clean_q:
        like = f"%{clean_q}%"
        base_where.append(
            """(
                c.consignment_number LIKE ? OR c.declaration_number LIKE ? OR
                c.trader_reference LIKE ? OR c.transport_document_number LIKE ? OR
                c.goods_description LIKE ? OR c.consignee_name LIKE ?
            )"""
        )
        base_params.extend([like] * 6)
    if date_clause:
        base_where.append(date_clause)
        base_params.extend(date_params)

    where = list(base_where)
    params = list(base_params)
    clean_status = status.strip().upper()
    if clean_status and clean_status != "ALL":
        where.append("UPPER(LTRIM(RTRIM(COALESCE(c.status, '')))) = ?")
        params.append(clean_status)

    header_join = f"LEFT JOIN [{schema}].[ENS_Headers] h ON h.declaration_number = c.declaration_number"
    filter_join = header_join if date_clause else ""

    try:
        expressions = consignment_reference_expressions(schema)

        # consignment_number is the TSS DEC reference and the one key present in
        # every tenant - CWF has no consignment_id - and it carries a unique index
        # in all four schemas. Ordering by it lets the page be taken straight off
        # that index with no sort, which is what keeps a deep page as cheap as a
        # shallow one across tenants holding 100k consignments.
        page_refs = [
            row["consignment_number"]
            for row in mirror_query_all(
                f"""
                SELECT c.consignment_number
                FROM [{schema}].[Consignments] c
                {filter_join}
                WHERE {' AND '.join(where)}
                ORDER BY c.consignment_number DESC
                OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
                """,
                params + [max(0, (page - 1) * size), size],
            )
            or []
        ]
        if page_refs:
            page_clause = "\n              AND c.consignment_number IN ({})".format(
                ", ".join("?" for _ in page_refs)
            )
            page_params: list[object] = list(page_refs)
        else:
            # Past the last page: ask for nothing rather than for everything.
            page_clause = "\n              AND 1 = 0"
            page_params = []

        rows = mirror_query_all(
            f"""
            SELECT TOP {size}
                {expressions['consignmentId']} AS ConsignmentRowID,
                c.consignment_number AS ConsignmentNumber,
                c.declaration_number AS DeclarationNumber,
                c.status AS Status,
                c.status AS TssStatus,
                c.control_status AS ControlStatus,
                c.error_message AS RejectReason,
                c.trader_reference AS TraderReference,
                c.transport_document_number AS TransportDocumentNumber,
                c.goods_description AS GoodsDescription,
                c.consignee_name AS ConsigneeName,
                c.destination_country AS DestinationCountry,
                c.movement_reference_number AS MovementReferenceNumber,
                c.total_packages AS TotalPackages,
                c.gross_mass_kg AS GrossMassKg,
                h.arrival_date_time AS ArrivalDateTime,
                h.arrival_date_time_utc AS ArrivalDateTimeUtc,
                h.arrival_port AS ArrivalPort,
                h.carrier_name AS CarrierName,
                {expressions['goodsCount']} AS GoodsItems,
                {expressions['sfd']} AS SfdReference,
                {expressions['sdi']} AS SdiReferences,
                c.updated_at AS UpdatedAt,
                {expressions['loadedAt']} AS LoadedAt
            FROM [{schema}].[Consignments] c
            {header_join}
            WHERE {' AND '.join(where)}{page_clause}
            ORDER BY c.consignment_number DESC
            """,
            params + page_params,
        )

        # The status tab counts are derived from every matching row, so a paged
        # query cannot produce them. This projection carries no goods subquery and
        # no SFD/SDI lookups, which is where the cost of the main query lives; it
        # takes the header join only when the date filter needs it.
        status_counts: dict[str, int] = {}
        for count_row in mirror_query_all(
            f"""
            SELECT UPPER(LTRIM(RTRIM(COALESCE(c.status, 'UNKNOWN')))) AS Status, COUNT(*) AS Total
            FROM [{schema}].[Consignments] c
            {filter_join}
            WHERE {' AND '.join(base_where)}
            GROUP BY UPPER(LTRIM(RTRIM(COALESCE(c.status, 'UNKNOWN'))))
            """,
            base_params,
        ) or []:
            status_counts[str(count_row.get("Status") or "UNKNOWN")] = int(count_row.get("Total") or 0)
        filtered_total = (
            status_counts.get(clean_status, 0)
            if clean_status and clean_status != "ALL"
            else sum(status_counts.values())
        )
        total_pages = max(1, (filtered_total + size - 1) // size) if filtered_total else 1
        current_page = min(page, total_pages)
        # SQL applied the ORDER BY and cut the page, so `rows` is the page.
        page_rows = rows
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    for row in page_rows:
        row["ClientCode"] = code
        # The mirror holds what TSS holds. Fusion's own validation summary is
        # computed from PRS rows in the Release 1 database and has no counterpart
        # here, so it is reported as absent rather than fabricated from TSS data.
        row["ValidationSummary"] = None
        row["ValidationStatus"] = ""
        row["MissingRequiredCount"] = 0
        row["AssumptionCount"] = 0
        row["PackageTypeMissingCount"] = 0
        row["PackageTypeNormalisedCount"] = 0

    return {
        "clientCode": code,
        "tenantSchema": schema,
        "source": f"Fusion_Flow_V3 [{schema}].Consignments",
        "consignments": page_rows,
        "statusVocabulary": status_vocabulary_rows(),
        "statusCounts": status_counts,
        "apiDateRange": date_range,
        "apiDateRangeOptions": [{"value": key, "label": label} for key, label in TSS_API_DATE_RANGE_OPTIONS],
        "pagination": {
            "page": current_page,
            "pageSize": size,
            "totalPages": total_pages,
            "filteredTotal": filtered_total,
            # Every filter reaches SQL, so the page is always cut there and the
            # totals are exact. Nothing is sampled and nothing is truncated.
            "sqlPaged": True,
        },
    }

@app.get("/api/consignments/{reference}")
def consignment_detail(reference: str, client_code: str = Query("PLE")) -> dict[str, object]:
    """One consignment from the tenant mirror, keyed by its TSS DEC reference.

    The path used to take an int because PRS.Consignment had a row id. The mirror
    does not: CWF has no consignment_id at all, and consignment_number - the DEC
    reference - is the only key every tenant shares. A numeric reference is still
    accepted for the tenants that do keep an id, so existing links do not break.
    """
    try:
        schema = tenant_schema(client_code)
    except UnknownTenant as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    lookup = str(reference or "").strip()
    if not lookup:
        raise HTTPException(status_code=422, detail="A consignment reference is required.")

    try:
        expressions = consignment_reference_expressions(schema)
        match_clause = "c.consignment_number = ?"
        match_params: list[object] = [lookup]
        if lookup.isdigit() and mirror_has_column(schema, "Consignments", "consignment_id"):
            match_clause = "(c.consignment_number = ? OR c.consignment_id = ?)"
            match_params = [lookup, int(lookup)]

        row = mirror_query_one(
            f"""
            SELECT TOP 1
                c.*,
                {expressions['consignmentId']} AS ConsignmentRowID,
                c.consignment_number AS ConsignmentNumber,
                c.declaration_number AS DeclarationNumber,
                c.status AS TssStatus,
                h.arrival_date_time AS HeaderArrivalDateTime,
                h.arrival_port AS HeaderArrivalPort,
                h.carrier_name AS HeaderCarrierName,
                h.carrier_eori AS HeaderCarrierEori,
                h.movement_type AS HeaderMovementType,
                {expressions['sfd']} AS SfdReference,
                {expressions['sdi']} AS SdiReferences
            FROM [{schema}].[Consignments] c
            LEFT JOIN [{schema}].[ENS_Headers] h ON h.declaration_number = c.declaration_number
            WHERE {match_clause}
            """,
            match_params,
        )
        goods = []
        if row:
            goods = mirror_query_all(
                f"""
                SELECT TOP 100
                    goods_id, consignment_number, declaration_number, commodity_code,
                    goods_description, type_of_packages, number_of_packages,
                    number_of_individual_pieces, package_marks, gross_mass_kg, net_mass_kg,
                    item_invoice_amount, item_invoice_currency, country_of_origin,
                    preference, controlled_goods, updated_at
                FROM [{schema}].[GoodsItems]
                WHERE consignment_number = ?
                ORDER BY goods_id
                """,
                [row["consignment_number"]],
            )
    except DbUnavailable as exc:
        raise db_error(exc) from exc

    if not row:
        raise HTTPException(status_code=404, detail=f"Consignment '{lookup}' was not found for tenant {schema}.")
    return {
        "consignment": row,
        "goodsItems": goods,
        "tenantSchema": schema,
        "source": f"Fusion_Flow_V3 [{schema}].Consignments",
        # Fusion's validation summary is computed from PRS rows in the Release 1
        # database. The mirror holds what TSS holds, so there is nothing to compute.
        "validationSummary": None,
    }

def selected_file_ordinal(profile: dict[str, object]) -> int:
    return required_file_ordinal(profile)


def upload_file_extension(upload_file: UploadFile | object) -> str:
    filename = str(getattr(upload_file, "filename", "") or "")
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def is_single_countrywide_pdf_invoice(profile: dict[str, object], uploaded_files: list[UploadFile]) -> bool:
    code = str(profile.get("portalClientCode") or profile.get("clientCode") or "").upper()
    return code == "CWD" and len(uploaded_files) == 1 and upload_file_extension(uploaded_files[0]) == "pdf"


def select_upload_file(uploaded_files: list[UploadFile], profile: dict[str, object]) -> tuple[UploadFile, int, str]:
    if is_single_countrywide_pdf_invoice(profile, uploaded_files):
        return uploaded_files[0], 1, "Map single Countrywide PDF invoice."

    ordinal = selected_file_ordinal(profile)
    return select_required_file(uploaded_files, profile), ordinal, f"Map attached file #{ordinal} for {profile['portalClientCode']}."


DEMO_UPLOAD_PROFILES: dict[str, dict[str, object]] = {
    "BKD": {
        "portalClientCode": "BKD",
        "clientCode": "BKD",
        "clientName": "Birkdale",
        "tssCredentialClientCode": "BKD",
        "preferredEnvCode": "TST",
        "defaultRoute": "A",
        "requiresEnsBeforeSubmit": True,
        "fileSelection": {
            "requiredFileOrdinal": 1,
            "acceptedExtensions": ".xlsx,.xls,.csv,.pdf",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "notes": "Birkdale demo maps the first uploaded file.",
        },
    },
}

DEMO_ENS_REFERENCES = {
    "PLE": "ENS900000000000001",
    "CWD": "ENS900000000000002",
    "BKD": "ENS900000000000003",
}


def demo_upload_profile(value: str) -> dict[str, object]:
    code = normalize_portal_code(value)
    fallback = fallback_profile(code)
    if fallback:
        fallback.setdefault("defaultRoute", "A")
        return fallback
    profile = DEMO_UPLOAD_PROFILES.get(code)
    if profile:
        return json.loads(json.dumps(profile))
    return {
        "portalClientCode": code,
        "clientCode": code,
        "clientName": code,
        "tssCredentialClientCode": code,
        "preferredEnvCode": "TST",
        "defaultRoute": "A",
        "requiresEnsBeforeSubmit": True,
        "fileSelection": {
            "requiredFileOrdinal": 1,
            "acceptedExtensions": ".xlsx,.xls,.csv,.pdf",
            "targetLandingTable": "ING.Inbound_File / ING.Raw_Record",
            "targetCanonicalRoot": "PRS.Consignment / PRS.Goods_Item",
            "notes": "Generic demo maps the first uploaded file.",
        },
    }


def demo_ens_payload(profile: dict[str, object], override_reference: str | None = None) -> dict[str, object]:
    code = str(profile.get("portalClientCode") or profile.get("clientCode") or "DEMO").upper()
    arrival = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0)
    declaration_number = (override_reference or DEMO_ENS_REFERENCES.get(code) or "ENS900000000000999").strip()
    return {
        "source": "demo_default",
        "declarationNumber": declaration_number,
        "declaration_number": declaration_number,
        "movementKey": f"DEMO-{code}-ENS-001",
        "movement_type": "IM",
        "transport_document_number": f"DEMO-{code}-TDN-001",
        "arrival_date_time": arrival.isoformat(),
        "arrival_port": "GBAUBELBELBEL",
        "place_of_loading": "IEDUBDUBDUB",
        "place_of_unloading": "GBAUBELBELBEL",
        "carrier_eori": "GB123456789000",
        "route": str(profile.get("defaultRoute") or "A"),
        "no_sfd_reason": "NONE",
    }


def apply_demo_ens_to_mapping(mapping_suggestions: dict[str, object], demo_ens: dict[str, object] | None) -> dict[str, object]:
    if not demo_ens:
        return mapping_suggestions
    supplied_target = {"targetTable": "PRS.Consignment", "targetColumn": "declaration_number", "source": "demoEns"}
    next_suggestions = dict(mapping_suggestions)
    next_suggestions["missingRequiredTargets"] = [
        item for item in list(mapping_suggestions.get("missingRequiredTargets") or [])
        if not (item.get("targetTable") == supplied_target["targetTable"] and item.get("targetColumn") == supplied_target["targetColumn"])
    ]
    next_suggestions["demoSatisfiedTargets"] = [supplied_target]
    return next_suggestions


def load_file_profile_column_map(profile: dict[str, object]) -> list[dict[str, object]]:
    return []


@app.post("/api/uploads/consignments/preview/validate")
def validate_upload_processing_preview(payload: dict[str, object] = Body(...)) -> dict[str, object]:
    client_code = str(payload.get("clientCode") or payload.get("client_code") or "BKD")
    demo_mode = bool(payload.get("demoMode") or payload.get("demo_mode") or False)
    profile = demo_upload_profile(client_code) if demo_mode else load_portal_profile(client_code)
    processing_preview = payload.get("processingPreview")
    if not isinstance(processing_preview, dict):
        raise HTTPException(status_code=422, detail="processingPreview is required for preview validation.")
    if demo_mode:
        product_master = {}
        product_master_context = {
            "source": "CFG.Product_Master",
            "available": False,
            "rowCount": 0,
            "reason": "Demo mode skips DB masterdata lookup.",
        }
    else:
        try:
            product_master, product_master_context = load_preview_product_master(profile)
        except DbUnavailable as exc:
            raise db_error(exc) from exc
    refreshed_preview = revalidate_processing_preview(
        processing_preview,
        profile=profile,
        product_master=product_master,
        apply_enrichment=True,
    )
    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": profile["clientCode"],
        "processingPreview": refreshed_preview,
        "demoMode": demo_mode,
        "databaseWrite": False,
        "tssWrite": False,
        "writeMode": "preview_validation_only",
        "validationContext": {
            "mode": "portal_edit_preview",
            "source": "Modules.Processing preview validation/enrichment",
            "note": "Edited preview values were revalidated and re-enriched without DB, PRS, STG, API, or TSS writes.",
            "productMaster": product_master_context,
            "packageTypeResolutionOrder": PACKAGE_TYPE_RESOLUTION_ORDER,
        },
    }


@app.post("/api/uploads/consignments/preview")
def upload_consignment_preview(
    client_code: Annotated[str, Form()] = "PLE",
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
    demo_mode: Annotated[bool, Form()] = False,
    demo_ens_reference: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    uploaded_files = list(files or [])
    if file is not None:
        uploaded_files.append(file)
    if not uploaded_files:
        raise HTTPException(status_code=422, detail="at least one file is required.")

    profile = demo_upload_profile(client_code) if demo_mode else load_portal_profile(client_code)
    code = str(profile["clientCode"])
    required_ordinal = selected_file_ordinal(profile)
    try:
        selected_file, selected_ordinal, selection_rule = select_upload_file(uploaded_files, profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    selected_content = selected_file.file.read()
    digest = hashlib.sha256(selected_content)
    size = len(selected_content)
    structure = inspect_upload(selected_file.filename, selected_content)
    column_mappings = load_file_profile_column_map(profile)
    mapping_summary = summarise_mapping(structure.get("columns", []), column_mappings)
    demo_ens = demo_ens_payload(profile, demo_ens_reference) if demo_mode else None
    mapping_suggestions = apply_demo_ens_to_mapping(suggest_column_mappings(structure.get("columns", [])), demo_ens)
    if demo_mode:
        product_master = {}
        product_master_context = {
            "source": "CFG.Product_Master",
            "available": False,
            "rowCount": 0,
            "reason": "Demo mode skips DB masterdata lookup.",
        }
    else:
        try:
            product_master, product_master_context = load_preview_product_master(profile)
        except DbUnavailable as exc:
            raise db_error(exc) from exc
    processing_preview = build_processing_preview(
        profile=profile,
        structure=structure,
        demo_ens=demo_ens,
        product_master=product_master,
    )

    received_files = [
        {
            "ordinal": index + 1,
            "filename": item.filename,
            "contentType": item.content_type,
            "selected": index + 1 == selected_ordinal,
        }
        for index, item in enumerate(uploaded_files)
    ]

    return {
        "portalClientCode": profile["portalClientCode"],
        "clientCode": code,
        "tssCredentialClientCode": profile["tssCredentialClientCode"],
        "fileSelection": profile["fileSelection"],
        "requiredFileOrdinal": required_ordinal,
        "selectedFileOrdinal": selected_ordinal,
        "selectionRule": selection_rule,
        "receivedFiles": received_files,
        "ignoredFiles": [item for item in received_files if not item["selected"]],
        "filename": selected_file.filename,
        "contentType": selected_file.content_type,
        "sizeBytes": size,
        "sha256": digest.hexdigest(),
        "detectedStructure": structure,
        "columnMappings": column_mappings,
        "mappingSummary": mapping_summary,
        "mappingSuggestions": mapping_suggestions,
        "processingPreview": processing_preview,
        "demoMode": bool(demo_mode),
        "demoEns": demo_ens,
        "databaseWrite": False,
        "tssWrite": False,
        "writeMode": "demo_preview_only" if demo_mode else "preview_only",
        "validationContext": {
            "ensSource": "demo_default" if demo_mode else "uploaded_or_existing",
            "demoSatisfiedTargets": mapping_suggestions.get("demoSatisfiedTargets", []),
            "productMaster": product_master_context,
            "packageTypeResolutionOrder": PACKAGE_TYPE_RESOLUTION_ORDER,
        },
        "wouldLand": {
            "fileTable": "ING.Inbound_File",
            "rowTable": "ING.Raw_Record",
            "sourceChannel": "MANUAL",
            "status": "NOT_WRITTEN" if demo_mode else "INGESTED",
            "note": "Demo mode validates only; no DB or TSS write is attempted." if demo_mode else "Preview only; portal upload writes remain disabled.",
        },
        "nextStep": "Review validation output only; demo mode does not land rows, create ENS, or submit to TSS." if demo_mode else "Use Module 1 ingestion/processing logic to land and transform rows before enabling DB writes from the portal.",
    }


PORTAL_DIST = Path(__file__).resolve().parents[2] / "fusion_portal" / "dist"

if PORTAL_DIST.exists():
    assets_dir = PORTAL_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="portal-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_portal(full_path: str = ""):
        reserved_prefixes = ("api/", "docs", "redoc", "openapi.json")
        if full_path.startswith(reserved_prefixes):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = (PORTAL_DIST / full_path).resolve()
        dist_root = PORTAL_DIST.resolve()
        try:
            candidate.relative_to(dist_root)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found") from None
        if candidate.is_file():
            return FileResponse(candidate)
        index_path = PORTAL_DIST / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Portal build not found")
        return FileResponse(index_path)
