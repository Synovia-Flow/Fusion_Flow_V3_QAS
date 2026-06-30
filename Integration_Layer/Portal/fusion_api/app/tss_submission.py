from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any

CONS_META_FIELDS = {
    "ConsignmentRowID",
    "EnsHeaderRowID",
    "ConsignmentOrdinal",
    "ExecutionID",
    "TransactionID",
    "ClientCode",
    "Status",
    "RejectReason",
    "MovementKey",
    "CreatedAt",
    "UpdatedAt",
    "HeaderDeclarationNumber",
    "HeaderArrivalDateTime",
    "HeaderMovementKey",
}

# Baseline from the local TSS v2.9.5 notes in the repo. These are not treated
# as optional: if any are missing we block live submit until mapping/enrichment
# provides them.
CONSIGNMENT_REQUIRED_FIELDS = (
    "declaration_number",
    "consignment_number",
    "goods_description",
    "transport_document_number",
    "controlled_goods",
    "consignor_eori",
    "consignee_eori",
    "importer_eori",
    "exporter_eori",
)


def compact(value: Any) -> Any:
    if isinstance(value, str):
        clean = value.strip()
        return clean or None
    return value


def normalise(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def is_consignment_step(step: dict[str, Any]) -> bool:
    resource = normalise(step.get("resourceName") or step.get("ResourceName"))
    endpoint = normalise(step.get("endpoint") or step.get("Endpoint"))
    operation = normalise(step.get("operationCode") or step.get("OperationCode"))
    return "consignment" in resource or "consignment" in endpoint or "consignment" in operation


def route_step_for(route: list[dict[str, Any]], op_type: str) -> tuple[int, dict[str, Any]] | None:
    wanted = normalise(op_type)
    for index, step in enumerate(route):
        operation = normalise(step.get("operationCode") or step.get("OperationCode"))
        current_op_type = normalise(step.get("opType") or step.get("OpType"))
        if not is_consignment_step(step):
            continue
        if current_op_type == wanted or operation.endswith("_" + wanted) or wanted in operation:
            return index, step
    return None


def ens_update_before_submit(route: list[dict[str, Any]]) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    update_match = route_step_for(route, "update")
    submit_match = route_step_for(route, "submit")
    if not update_match or not submit_match:
        return False, update_match[1] if update_match else None, submit_match[1] if submit_match else None
    return update_match[0] < submit_match[0], update_match[1], submit_match[1]


def step_endpoint(step: dict[str, Any] | None) -> str:
    return str((step or {}).get("endpoint") or (step or {}).get("Endpoint") or "/consignments")


def step_method(step: dict[str, Any] | None) -> str:
    return str((step or {}).get("httpMethod") or (step or {}).get("HttpMethod") or "POST")


def non_empty_payload(row: dict[str, Any], *, op_type: str, ens_value: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"op_type": op_type}
    for key, value in row.items():
        if key in CONS_META_FIELDS:
            continue
        cleaned = compact(value)
        if cleaned is not None:
            payload[key] = cleaned
    if ens_value:
        payload["declaration_number"] = ens_value
    return payload


def missing_required(payload: dict[str, Any], goods_count: int) -> list[str]:
    missing = [field for field in CONSIGNMENT_REQUIRED_FIELDS if compact(payload.get(field)) is None]
    if goods_count <= 0:
        missing.append("goods_items")
    return missing


def build_consignment_submission_plan(
    *,
    profile: dict[str, Any],
    consignment: dict[str, Any],
    goods_items: list[dict[str, Any]],
    route: list[dict[str, Any]],
) -> dict[str, Any]:
    ens_value = compact(consignment.get("declaration_number")) or compact(consignment.get("HeaderDeclarationNumber"))
    update_payload = non_empty_payload(consignment, op_type="update", ens_value=ens_value)
    submit_payload = {
        "op_type": "submit",
        "declaration_number": ens_value,
        "consignment_number": compact(consignment.get("consignment_number")),
    }
    submit_payload = {k: v for k, v in submit_payload.items() if compact(v) is not None}
    missing = missing_required(update_payload, len(goods_items))
    ens_step_first, update_step, submit_step = ens_update_before_submit(route)
    submit_allowed = not missing and ens_step_first and bool(profile.get("requiresEnsBeforeSubmit", True))

    return {
        "ready": submit_allowed,
        "missing": missing,
        "ensDeclarationNumber": ens_value,
        "goodsItemCount": len(goods_items),
        "routeIsEnsFirst": ens_step_first,
        "routeBlockers": [] if ens_step_first else ["UPDATE_CONSIGNMENT_WITH_ENS must be configured before SUBMIT_CONSIGNMENT."],
        "steps": [
            {
                "operationCode": "UPDATE_CONSIGNMENT_WITH_ENS",
                "endpoint": step_endpoint(update_step),
                "httpMethod": step_method(update_step),
                "payload": update_payload,
            },
            {
                "operationCode": "SUBMIT_CONSIGNMENT",
                "endpoint": step_endpoint(submit_step),
                "httpMethod": step_method(submit_step),
                "payload": submit_payload,
            },
        ],
        "invariant": "UPDATE_CONSIGNMENT_WITH_ENS must run before SUBMIT_CONSIGNMENT.",
    }


def post_tss_json(*, base_url: str, username: str, password: str, endpoint: str, payload: dict[str, Any], timeout: int = 45) -> dict[str, Any]:
    url = base_url.rstrip("/") + endpoint
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    started = time.perf_counter()
    status: int | None = None
    response_text = ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - CFG owns trusted TSS base URLs.
            status = int(response.status)
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        status = int(error.code)
        response_text = error.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        response_text = str(error.reason)

    return {
        "statusCode": status,
        "ok": status is not None and 200 <= status < 300,
        "durationMs": int((time.perf_counter() - started) * 1000),
        "responseText": response_text[:8000],
    }
