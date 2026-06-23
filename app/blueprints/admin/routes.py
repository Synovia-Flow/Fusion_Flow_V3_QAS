# ============================================================
# app/blueprints/admin/routes.py - Fusion Flow V2
# Admin panel: view and edit [{tenant_schema}].AppConfiguration.
#
# Active tenant resolved from flask.session via app.tenant.get_tenant().
# One implementation handles all tenants - no per-tenant copies.
#
# Auth: protected by the global _require_login middleware in
# app/__init__.py - no additional decorator needed here.
# ============================================================

from flask import render_template, request, redirect, url_for, flash, jsonify

from app.blueprints.admin import admin_bp
from app.db import get_db
from app import config_store
from app.tenant import get_tenant


CATEGORY_META = {
    "TSS_API": {
        "label": "TSS Portal API",
        "icon": "bi-arrow-left-right",
        "description": "Credentials and endpoints for the Trader Support Service REST API.",
    },
    "SMTP": {
        "label": "Email / SMTP",
        "icon": "bi-envelope",
        "description": "Outbound email settings used for SDI deadline alerts.",
    },
    "INGEST_AUTO": {
        "label": "Invoice Auto-Staging",
        "icon": "bi-file-earmark-arrow-up",
        "description": "Defaults and toggles for auto-creating one ENS per email batch, with one consignment per invoice and goods per line.",
    },
    "SDI_AUTO": {
        "label": "SDI / SupDec Automation",
        "icon": "bi-lightning-charge",
        "description": "Controls the one-step Supplementary Declaration worker after TSS exposes the SFD/SUP relationship.",
    },
    "IMAP": {
        "label": "Inbound Email / IMAP",
        "icon": "bi-inbox",
        "description": "Legacy mailbox polling settings. Hidden from the admin UI; Microsoft Graph is the preferred inbound email path.",
    },
    "GRAPH": {
        "label": "Inbound Email / Microsoft Graph",
        "icon": "bi-microsoft",
        "description": "Inbound mailbox pickup using Microsoft Graph application credentials.",
    },
    "VALIDATION": {
        "label": "Validation Controls",
        "icon": "bi-shield-check",
        "description": "Runtime switches that decide how much local validation blocks before TSS receives data.",
    },
    "NOTIFY": {
        "label": "Email Automation Notifications",
        "icon": "bi-bell",
        "description": "Controls for email automation smoke-test, error and final movement notifications.",
    },
}

CATEGORY_ORDER = ["TSS_API", "SMTP", "GRAPH", "INGEST_AUTO", "SDI_AUTO", "VALIDATION", "NOTIFY"]

HIDDEN_CONFIG_CATEGORIES = {"IMAP"}
HIDDEN_CONFIG_KEYS = {
    ("SDI_AUTO", "DRY_RUN"),
}

BOOLEAN_CONFIG_KEYS = {
    ("GRAPH", "ENABLED"),
    ("GRAPH", "UNREAD_ONLY"),
    ("DEMO", "ENABLED"),
    ("IMAP", "ENABLED"),
    ("INGEST_AUTO", "AUTO_VALIDATE"),
    ("INGEST_AUTO", "ENABLED"),
    ("SDI_AUTO", "DRY_RUN"),
    ("SDI_AUTO", "SUBMIT_ENABLED"),
    ("SMTP", "ENABLED"),
    ("NOTIFY", "ENS_RECEIVED_ENABLED"),
    ("NOTIFY", "CONSIGNMENTS_RECEIVED_ENABLED"),
    ("NOTIFY", "STAGING_FAILURES_ENABLED"),
    ("NOTIFY", "MOVEMENT_AUTHORISED_ENABLED"),
    ("NOTIFY", "ENS_PACK_AUTO_ENABLED"),
    ("VALIDATION", "STRICT_MASTERDATA"),
    ("VALIDATION", "STRICT_MASTERDATA_VALIDATION"),
}

BOOLEAN_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
BOOLEAN_FALSE_VALUES = {"0", "false", "no", "n", "off"}

CHOICE_CONFIG_KEYS = {
    ("TSS_API", "ENVIRONMENT"): [
        ("production", "production"),
        ("test", "test"),
        ("demo", "demo - simulated TSS responses"),
    ],
    ("INGEST_AUTO", "MODE"): [
        ("review_required", "review_required - review editable drafts first"),
        ("auto_create_if_clean", "auto_create_if_clean - trusted clean flows can auto-create"),
    ],
}

CONFIG_DESCRIPTIONS = {
    ("TSS_API", "BASE_URL"): "Production TSS API base URL used when ENVIRONMENT is production.",
    ("TSS_API", "TEST_URL"): "Test/QAS TSS API base URL used when ENVIRONMENT is test.",
    ("TSS_API", "ENVIRONMENT"): "Active TSS target. Use production for live declarations, test for QAS, or demo for simulated end-to-end demos with no live TSS calls.",
    ("TSS_API", "USERNAME"): "TSS API username for this tenant.",
    ("TSS_API", "PASSWORD"): "TSS API password for this tenant.",
    ("TSS_API", "ACT_AS"): "Optional default TSS actAs customer_account_sys_id for delegated API calls. This overrides TSS_API_ACT_AS env when set.",
    ("DEMO", "ENABLED"): "Legacy compatibility flag. Prefer TSS_API.ENVIRONMENT=demo for simulated TSS API responses.",
    ("SMTP", "ENABLED"): "When true, outbound SMTP email sending is enabled.",
    ("SMTP", "SERVER"): "SMTP host used for deadline alerts and operational emails.",
    ("SMTP", "PORT"): "SMTP port. Office 365 STARTTLS usually uses 587.",
    ("SMTP", "SENDER_EMAIL"): "Mailbox address used as the sender for outbound alerts.",
    ("SMTP", "SENDER_PASSWORD"): "Password or app password for the sender mailbox.",
    ("SMTP", "ALERT_RECIPIENT"): "Default recipient for SDI deadline and operational alert emails.",
    ("INGEST_AUTO", "ENABLED"): "This is the switch behind the 'Record creation is disabled' warning. Set it to true/Enabled to let Confirm create local ENS, consignments and goods. It does not submit records to TSS.",
    ("INGEST_AUTO", "MODE"): "Default upload mode. review_required keeps parsed records editable before creation; auto_create_if_clean only creates clean trusted batches automatically.",
    ("INGEST_AUTO", "AUTO_VALIDATE"): "Runs local validation after invoice/email staging. This does not unlock record creation by itself; INGEST_AUTO.ENABLED must also be true.",
    ("INGEST_AUTO", "DEFAULT_MOVEMENT_TYPE"): "Fallback ENS movement_type used when email metadata does not provide one.",
    ("INGEST_AUTO", "DEFAULT_IDENTITY_NO_OF_TRANSPORT"): "Fallback transport identity used when inbound data is missing the vehicle/vessel reference.",
    ("INGEST_AUTO", "DEFAULT_NATIONALITY_OF_TRANSPORT"): "Fallback two-letter transport nationality code.",
    ("INGEST_AUTO", "DEFAULT_ARRIVAL_PORT"): "Fallback ENS arrival port code for auto-staged batches.",
    ("INGEST_AUTO", "DEFAULT_PLACE_OF_LOADING"): "Fallback ENS place of loading.",
    ("INGEST_AUTO", "DEFAULT_PLACE_OF_UNLOADING"): "Fallback ENS place of unloading.",
    ("INGEST_AUTO", "DEFAULT_TRANSPORT_CHARGES"): "Fallback TSS transport charges code.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_EORI"): "Fallback ENS carrier EORI.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_NAME"): "Fallback ENS carrier name.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_STREET_NUMBER"): "Fallback ENS carrier street and number.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_CITY"): "Fallback ENS carrier city.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_POSTCODE"): "Fallback ENS carrier postcode.",
    ("INGEST_AUTO", "DEFAULT_CARRIER_COUNTRY"): "Fallback ENS carrier country code.",
    ("INGEST_AUTO", "DEFAULT_HAULIER_EORI"): "Optional fallback haulier EORI for movement metadata.",
    ("INGEST_AUTO", "DEFAULT_CONTAINER_INDICATOR"): "Fallback container indicator. Use 0 for uncontainerised or 1 for containerised where required.",
    ("INGEST_AUTO", "DEFAULT_CONTROLLED_GOODS"): "Fallback controlled goods flag used when source data has no controlled goods signal.",
    ("INGEST_AUTO", "DEFAULT_GOODS_DOMESTIC_STATUS"): "Fallback goods domestic status code for consignments.",
    ("INGEST_AUTO", "DEFAULT_COUNTRY_OF_ORIGIN"): "Fallback goods country of origin when product master does not provide one.",
    ("INGEST_AUTO", "DEFAULT_PACKAGE_TYPE"): "Fallback goods package type code.",
    ("INGEST_AUTO", "DEFAULT_PROCEDURE_CODE"): "Fallback goods procedure code.",
    ("INGEST_AUTO", "DEFAULT_ADDITIONAL_PROCEDURE_CODE"): "Fallback goods additional procedure code.",
    ("INGEST_AUTO", "DEFAULT_VALUATION_METHOD"): "Fallback goods valuation method.",
    ("INGEST_AUTO", "DEFAULT_INVOICE_CURRENCY"): "Fallback invoice currency for parsed invoice lines.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_EORI"): "TSS-registered importer EORI to use when ingestion is creating the SFD path. If blank, Company Master EORI is used when available.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_NAME"): "Fallback importer legal name used with DEFAULT_IMPORTER_EORI when TSS requires address data.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_STREET_NUMBER"): "Fallback importer street and number used with DEFAULT_IMPORTER_EORI.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_CITY"): "Fallback importer city used with DEFAULT_IMPORTER_EORI.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_POSTCODE"): "Fallback importer postcode used with DEFAULT_IMPORTER_EORI.",
    ("INGEST_AUTO", "DEFAULT_IMPORTER_COUNTRY"): "Fallback importer country code used with DEFAULT_IMPORTER_EORI.",
    ("INGEST_AUTO", "DEFAULT_CONSIGNOR_EORI"): "Fallback consignor EORI used when the source file does not provide one.",
    ("INGEST_AUTO", "DEFAULT_EXPORTER_EORI"): "Fallback exporter EORI used when the source file does not provide one.",
    ("INGEST_AUTO", "DEFAULT_ARRIVAL_HOURS_AHEAD"): "When no arrival date is provided, stage arrival this many hours after now.",
    ("INGEST_AUTO", "SUPPLIER_NAME"): "Default supplier name used for exporter/consignor fallback matching.",
    ("SDI_AUTO", "DRY_RUN"): "Legacy safety flag for direct script runs. Hidden from the admin UI; AUTOSUBMIT TSS is the operational control.",
    ("SDI_AUTO", "SUBMIT_ENABLED"): "AUTOSUBMIT TSS. When false, Fusion only discovers/stages SDIs automatically and live TSS submit is manual only. When true, the automatic sync runs update_sdi_goods, update_sdi and submit_sdi against TSS.",
    ("SDI_AUTO", "MAX_ITEMS"): "Maximum SFD candidates processed by one SDI autosubmit run.",
    ("IMAP", "ENABLED"): "When true, the IMAP polling worker can read inbound mailbox messages.",
    ("IMAP", "HOST"): "IMAP server host for inbound invoice mailbox polling.",
    ("IMAP", "PORT"): "IMAP SSL port, usually 993.",
    ("IMAP", "USERNAME"): "Inbound IMAP mailbox username.",
    ("IMAP", "PASSWORD"): "Inbound IMAP mailbox password.",
    ("IMAP", "FOLDER"): "Mailbox folder to scan, usually INBOX.",
    ("IMAP", "PROCESSED_FOLDER"): "Optional folder to move processed messages into.",
    ("IMAP", "SEARCH"): "IMAP search expression, for example UNSEEN.",
    ("GRAPH", "ENABLED"): "When true, Microsoft Graph mailbox polling can read inbound messages.",
    ("GRAPH", "TENANT_ID"): "Microsoft Entra tenant id used for Graph client credentials.",
    ("GRAPH", "CLIENT_ID"): "Microsoft Graph application client id.",
    ("GRAPH", "CLIENT_SECRET"): "Microsoft Graph application client secret.",
    ("GRAPH", "MAILBOX"): "Mailbox UPN or email address to poll via Microsoft Graph.",
    ("GRAPH", "FOLDER"): "Folder id, well-known folder name, or root display name to scan.",
    ("GRAPH", "PROCESSED_FOLDER"): "Optional processed folder id, well-known name, or root display name.",
    ("GRAPH", "UNREAD_ONLY"): "When true, Graph polling only picks unread messages with attachments.",
    ("GRAPH", "MAX_MESSAGES"): "Maximum Graph messages to inspect in one polling pass.",
    ("GRAPH", "ALLOWED_SENDER_DOMAINS"): "Comma-separated sender domains allowed for Graph ingestion. BKD defaults to birkdalesales.com.",
    ("VALIDATION", "STRICT_MASTERDATA"): "When true, local validation requires masterdata matches before staging or submission can continue.",
    ("VALIDATION", "STRICT_MASTERDATA_VALIDATION"): "When true, local validation blocks unknown or mismatched parties/carriers before TSS submission.",
    ("NOTIFY", "ENS_RECEIVED_ENABLED"): "When true, sends temporary smoke-test email when a DETAILS FOR email creates an ENS header.",
    ("NOTIFY", "CONSIGNMENTS_RECEIVED_ENABLED"): "When true, sends temporary smoke-test email when a Sales Orders XLSX creates consignments and goods.",
    ("NOTIFY", "STAGING_FAILURES_ENABLED"): "When true, sends operational email when ingestion/staging needs manual action.",
    ("NOTIFY", "MOVEMENT_AUTHORISED_ENABLED"): "When true, sends final email when ENS header and all consignments reach AUTHORISED_FOR_MOVEMENT.",
    ("NOTIFY", "ENS_PACK_AUTO_ENABLED"): "When true, automatically sends the ENS movement pack after the Authorised for Movement notification succeeds.",
    ("NOTIFY", "EMAIL_AUTOMATION_TEST_TO"): "Recipient list for temporary positive smoke-test notifications.",
    ("NOTIFY", "STAGING_FAILURES_TO"): "Recipient list for ingestion/staging error notifications. Falls back to SMTP_NOTIFY_TO when blank.",
    ("NOTIFY", "MOVEMENT_AUTHORISED_TO"): "Recipient list for final Authorised for Movement notifications. Falls back to SMTP_NOTIFY_TO when blank.",
    ("NOTIFY", "MOVEMENT_AUTHORISED_CC"): "Always-CC recipient list for final Authorised for Movement notifications.",
    ("NOTIFY", "ENS_PACK_AUTO_TO"): "Recipient list for automatic ENS movement pack emails. Must be explicitly configured; it does not fall back to SMTP_NOTIFY_TO.",
    ("NOTIFY", "ENS_PACK_AUTO_CC"): "Optional CC recipient list for automatic ENS movement pack emails.",
}

VISIBLE_CONFIG_KEYS = {
    "TSS_API": ["BASE_URL", "TEST_URL", "ENVIRONMENT", "USERNAME", "PASSWORD", "ACT_AS"],
    "SDI_AUTO": ["SUBMIT_ENABLED", "MAX_ITEMS"],
    "NOTIFY": [
        "ENS_RECEIVED_ENABLED",
        "CONSIGNMENTS_RECEIVED_ENABLED",
        "STAGING_FAILURES_ENABLED",
        "MOVEMENT_AUTHORISED_ENABLED",
        "ENS_PACK_AUTO_ENABLED",
        "EMAIL_AUTOMATION_TEST_TO",
        "STAGING_FAILURES_TO",
        "MOVEMENT_AUTHORISED_TO",
        "MOVEMENT_AUTHORISED_CC",
        "ENS_PACK_AUTO_TO",
        "ENS_PACK_AUTO_CC",
    ],
}

DEFAULT_CONFIG_VALUES = {
    ("NOTIFY", "ENS_RECEIVED_ENABLED"): "true",
    ("NOTIFY", "CONSIGNMENTS_RECEIVED_ENABLED"): "true",
    ("NOTIFY", "STAGING_FAILURES_ENABLED"): "true",
    ("NOTIFY", "MOVEMENT_AUTHORISED_ENABLED"): "true",
    ("NOTIFY", "ENS_PACK_AUTO_ENABLED"): "false",
    ("NOTIFY", "EMAIL_AUTOMATION_TEST_TO"): "alvaro.molina@synoviadigital.com",
    ("SDI_AUTO", "DRY_RUN"): "true",
    ("SDI_AUTO", "SUBMIT_ENABLED"): "false",
    ("SDI_AUTO", "MAX_ITEMS"): "25",
}

CONFIG_DISPLAY_KEYS = {
    ("TSS_API", "USERNAME"): "USER",
    ("SDI_AUTO", "SUBMIT_ENABLED"): "AUTOSUBMIT TSS",
}

SECRET_CONFIG_KEYS = {
    ("TSS_API", "PASSWORD"),
    ("SMTP", "SENDER_PASSWORD"),
    ("IMAP", "PASSWORD"),
    ("GRAPH", "CLIENT_SECRET"),
}


def _resolve_tss_settings():
    """Resolve tenant TSS settings using the shared TSS resolver."""
    from app.tss_api import resolve_tss_settings
    return resolve_tss_settings()


def _is_boolean_config(category, config_key, value=None):
    text = str(value or "").strip().lower()
    return (category, config_key) in BOOLEAN_CONFIG_KEYS or text in {"true", "false"}


def _display_boolean_value(value):
    text = str(value or "").strip().lower()
    if text in BOOLEAN_TRUE_VALUES:
        return "true"
    if text in BOOLEAN_FALSE_VALUES:
        return "false"
    return text


def _config_choices(category, config_key):
    return [
        {"value": value, "label": label}
        for value, label in CHOICE_CONFIG_KEYS.get((category, config_key), [])
    ]


def _choice_config_values(category, config_key):
    return {item["value"] for item in _config_choices(category, config_key)}


def _config_description(category, config_key, description):
    text = (description or "").strip()
    if text:
        return text
    return CONFIG_DESCRIPTIONS.get(
        (category, config_key),
        f"Custom tenant setting for {category}.{config_key}. Add a database description if this becomes a permanent control.",
    )


def _build_config_row(row):
    category = row[1]
    config_key = row[2]
    value = row[3] or ""
    is_boolean = _is_boolean_config(category, config_key, value)
    return {
        "id": row[0],
        "category": category,
        "key": config_key,
        "display_key": CONFIG_DISPLAY_KEYS.get((category, config_key), config_key),
        "value": _display_boolean_value(value) if is_boolean else value,
        "description": _config_description(category, config_key, row[4]),
        "has_db_description": bool((row[4] or "").strip()),
        "is_secret": bool(row[5]),
        "updated_at": row[6],
        "is_boolean": is_boolean,
        "choices": _config_choices(category, config_key),
    }


def _build_missing_config_row(category, config_key):
    return _build_config_row((
        None,
        category,
        config_key,
        DEFAULT_CONFIG_VALUES.get((category, config_key), ""),
        "",
        1 if (category, config_key) in SECRET_CONFIG_KEYS else 0,
        None,
    ))


def _ensure_visible_config_rows(grouped):
    for category, keys in VISIBLE_CONFIG_KEYS.items():
        rows = grouped.setdefault(category, [])
        present = {row["key"] for row in rows}
        for key in keys:
            if key not in present:
                rows.append(_build_missing_config_row(category, key))
                present.add(key)


def _admin_smtp_test_to(grouped):
    smtp_rows = grouped.get("SMTP") or []
    values = {row["key"]: (row.get("value") or "").strip() for row in smtp_rows}
    return (
        values.get("ALERT_RECIPIENT")
        or values.get("SENDER_EMAIL")
        or "nexus@synoviaintegration.com"
    )


def _should_show_config_row(row):
    return (
        row[1] not in HIDDEN_CONFIG_CATEGORIES
        and (row[1], row[2]) not in HIDDEN_CONFIG_KEYS
        and not (row[1] == "DEMO" and row[2] == "ENABLED")
    )


@admin_bp.route("/")
def index():
    return redirect(url_for("admin.settings"))


@admin_bp.route("/settings")
def settings():
    tenant = get_tenant()
    schema = tenant["schema"]

    conn = get_db()
    cursor = conn.cursor()
    table_missing = False
    rows = []
    try:
        cursor.execute(
            f"""
            SELECT id, category, config_key, config_value,
                   description, is_secret, updated_at
            FROM [{schema}].AppConfiguration
            ORDER BY category, config_key
            """
        )
        rows = cursor.fetchall()
    except Exception:
        table_missing = True

    grouped = {}
    for row in rows:
        if not _should_show_config_row(row):
            continue
        grouped.setdefault(row[1], []).append(_build_config_row(row))
    if not table_missing:
        _ensure_visible_config_rows(grouped)

    categories = []
    seen = set()
    for cat in CATEGORY_ORDER:
        if cat in grouped:
            categories.append((cat, grouped[cat]))
            seen.add(cat)
    for cat, cat_rows in grouped.items():
        if cat not in seen:
            categories.append((cat, cat_rows))

    return render_template(
        "admin/settings.html",
        categories=categories,
        category_meta=CATEGORY_META,
        table_missing=table_missing,
        tss_resolution=_resolve_tss_settings(),
        smtp_test_to=_admin_smtp_test_to(grouped),
    )


@admin_bp.route("/settings/save", methods=["POST"])
def save_settings():
    """Upsert one or more AppConfiguration rows for active tenant's schema."""
    category = request.form.get("category", "").strip()
    if not category:
        flash("Invalid form submission - missing category.", "danger")
        return redirect(url_for("admin.settings"))

    tenant = get_tenant()
    schema = tenant["schema"]

    conn = get_db()
    cursor = conn.cursor()

    grouped_updates = {}
    for key, value in request.form.items():
        if not key.startswith("field_"):
            continue
        raw_key = key[len("field_"):]
        if "__" in raw_key:
            row_category, config_key = raw_key.split("__", 1)
        else:
            row_category, config_key = category, raw_key
        row_category = row_category.strip()
        config_key = config_key.strip()
        if not row_category or row_category in HIDDEN_CONFIG_CATEGORIES or not config_key:
            continue
        grouped_updates.setdefault(row_category, {})[config_key] = value

    if not grouped_updates:
        flash("Nothing to save.", "warning")
        return redirect(url_for("admin.settings"))

    errors = []
    saved_categories = []
    for row_category, updates in grouped_updates.items():
        cursor.execute(
            f"""
            SELECT config_key, config_value
            FROM [{schema}].AppConfiguration
            WHERE category = ?
            """,
            row_category,
        )
        existing_values = {row[0]: row[1] for row in cursor.fetchall()}

        for config_key, value in updates.items():
            if value == "__secret_unchanged__":
                continue
            cleaned_value = value.strip()
            choice_values = _choice_config_values(row_category, config_key)
            if choice_values and cleaned_value not in choice_values:
                errors.append(f"{row_category}.{config_key}: value must be one of {', '.join(sorted(choice_values))}")
                continue
            if _is_boolean_config(row_category, config_key, existing_values.get(config_key)):
                normalized = cleaned_value.lower()
                if normalized not in {"true", "false"}:
                    errors.append(f"{row_category}.{config_key}: value must be exactly true or false")
                    continue
                cleaned_value = normalized
            try:
                cursor.execute(
                    f"""
                    UPDATE [{schema}].AppConfiguration
                    SET config_value = ?, updated_at = GETUTCDATE()
                    WHERE category = ? AND config_key = ?
                    """,
                    cleaned_value,
                    row_category,
                    config_key,
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        f"""
                        INSERT INTO [{schema}].AppConfiguration
                            (category, config_key, config_value, description, is_secret)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        row_category,
                        config_key,
                        cleaned_value,
                        _config_description(row_category, config_key, None),
                        1 if (row_category, config_key) in SECRET_CONFIG_KEYS else 0,
                    )
                if row_category not in saved_categories:
                    saved_categories.append(row_category)
            except Exception as exc:
                errors.append(f"{row_category}.{config_key}: {exc}")

    if errors:
        conn.rollback()
        flash(f"Save failed: {'; '.join(errors)}", "danger")
    else:
        conn.commit()
        config_store.reload(tenant["code"])
        if category == "__all__":
            flash("All visible settings saved successfully.", "success")
        else:
            label = CATEGORY_META.get(category, {}).get("label", category)
            flash(f"{label} settings saved successfully.", "success")

    return redirect(url_for("admin.settings"))


@admin_bp.route("/settings/test-tss", methods=["POST"])
def test_tss_connection():
    """Connectivity probe against TSS API with tenant config + env fallback."""
    try:
        from app.tss_api import TssApiClient, build_cfg_client

        resolved = _resolve_tss_settings()
        if resolved.get("demo_enabled"):
            client = build_cfg_client()
            result = client.test_connection()
            return jsonify({
                "ok": bool(result.get("success")),
                "message": "Demo Mode is enabled. Fusion is using simulated TSS responses; no live TSS API call was made.",
            }), 200

        base_url = resolved["base_url"]
        username = resolved["username"]
        password = resolved["password"]

        if not base_url or not username or not password:
            return jsonify({
                "ok": False,
                "message": "TSS API settings are incomplete. Fill AppConfiguration or provide TSS_API_* environment variables.",
            }), 200

        client = TssApiClient(base_url=base_url, username=username, password=password)
        result = client.test_connection()

        if result.get("http_status") == 200:
            return jsonify({
                "ok": True,
                "message": f"Connected - TSS API responded successfully using {resolved['environment']} mode ({resolved['source_label']}).",
            })

        return jsonify({
            "ok": False,
            "message": (
                f"TSS API reachable but configuration is not valid for requests "
                f"(HTTP {result.get('http_status')}). Check the selected URL and credentials."
            ),
        }), 200

    except ValueError as exc:
        msg = str(exc)
        if "html" in msg.lower():
            return jsonify({
                "ok": False,
                "message": "Authentication failed or wrong BASE_URL - server returned an HTML login page. Check BASE_URL / TEST_URL and PASSWORD.",
            }), 200
        return jsonify({"ok": False, "message": msg}), 200

    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 200
