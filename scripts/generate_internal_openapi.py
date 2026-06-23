#!/usr/bin/env python3
"""
Generate OpenAPI 3.0 specs for Fusion Flow's internal Flask endpoints.

The generator imports the real Flask applications to read their URL maps, then
uses AST analysis of each route function to infer request parameters, request
bodies, response media types, and common response status codes from the code.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "api"
STATIC_METHODS = {"HEAD", "OPTIONS"}


def _ensure_import_path() -> None:
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_apps() -> list[dict[str, Any]]:
    _ensure_import_path()
    from app import create_app
    from ingest_service import create_ingest_app

    return [
        {
            "name": "Fusion Flow Portal",
            "slug": "fusion-flow-portal",
            "factory": "app.create_app",
            "app": create_app("testing"),
            "auth": "portal",
            "description": (
                "Internal Flask portal for ENS, consignments, goods, SFD, SDI, "
                "GMR, operations, orchestrator, analytics, master data and ingest bridge."
            ),
        },
        {
            "name": "Fusion Ingest Service",
            "slug": "fusion-ingest-service",
            "factory": "ingest_service.create_ingest_app",
            "app": create_ingest_app("testing"),
            "auth": "ingest",
            "description": (
                "Standalone Flask ingest service for document intelligence, PDF ingestion, "
                "company enrichment, Synovia upload and master-data remediation."
            ),
        },
    ]


def _rel(path: str | os.PathLike[str]) -> str:
    try:
        return str(Path(path).resolve().relative_to(ROOT)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/")


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_int(node: ast.AST | None) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _is_request_get_json(call: ast.Call) -> bool:
    return _call_name(call.func) in {"request.get_json", "get_json"} or _call_name(call.func).endswith(
        ".request.get_json"
    )


def _unwrap_or_call(node: ast.AST) -> ast.Call | None:
    if isinstance(node, ast.Call):
        return node
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            call = _unwrap_or_call(value)
            if call:
                return call
    return None


class RouteAnalyzer(ast.NodeVisitor):
    """Static, intentionally conservative Flask route analyser."""

    REQUEST_COLLECTIONS = {"args", "form", "files", "values", "headers", "cookies"}

    def __init__(self, request_aliases: dict[str, str] | None = None, json_aliases: set[str] | None = None) -> None:
        self.params: dict[str, set[str]] = {k: set() for k in self.REQUEST_COLLECTIONS}
        self.getlist: dict[str, set[str]] = {k: set() for k in self.REQUEST_COLLECTIONS}
        self.json_fields: set[str] = set()
        self.json_vars: set[str] = set(json_aliases or set())
        self.request_aliases: dict[str, str] = dict(request_aliases or {})
        self.helper_calls: list[ast.Call] = []
        self.status_codes: set[int] = set()
        self.response_kinds: set[str] = set()
        self.templates: set[str] = set()
        self.redirects = False
        self.uses_json_request = False
        self.uses_webhook_key = False
        self.uses_csrf_token = False
        self.uses_subprocess = False

    def visit_Assign(self, node: ast.Assign) -> Any:
        call = _unwrap_or_call(node.value)
        if call and _is_request_get_json(call):
            self.uses_json_request = True
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.json_vars.add(target.id)
        collection = self._request_collection_for_node(node.value)
        if not collection and isinstance(node.value, ast.Call):
            for arg in node.value.args:
                collection = self._request_collection_for_node(arg)
                if collection:
                    break
        if collection:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.request_aliases[target.id] = collection
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        call = _unwrap_or_call(node.value) if node.value else None
        if call and _is_request_get_json(call) and isinstance(node.target, ast.Name):
            self.uses_json_request = True
            self.json_vars.add(node.target.id)
        collection = self._request_collection_for_node(node.value) if node.value else None
        if not collection and isinstance(node.value, ast.Call):
            for arg in node.value.args:
                collection = self._request_collection_for_node(arg)
                if collection:
                    break
        if collection and isinstance(node.target, ast.Name):
            self.request_aliases[node.target.id] = collection
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        name = _call_name(node.func)

        self._capture_request_access(node, name)
        self._capture_json_field_access(node, name)
        self._capture_response(node, name)
        if isinstance(node.func, ast.Name):
            self.helper_calls.append(node)

        if name.endswith("subprocess.run") or name == "subprocess.run":
            self.uses_subprocess = True

        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        name = _call_name(node.value)
        key = _const_str(node.slice)
        if key:
            self._add_request_field(name, key, is_list=False)
            self._add_json_field(name, key)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> Any:
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            key = _const_str(node.left)
            if key and node.comparators:
                name = _call_name(node.comparators[0])
                self._add_request_field(name, key, is_list=False)
        self.generic_visit(node)

    def _capture_request_access(self, node: ast.Call, name: str) -> None:
        method = name.rsplit(".", 1)[-1]
        base = name.rsplit(".", 1)[0] if "." in name else ""
        field = _const_str(node.args[0]) if node.args else None
        if not field:
            return
        is_list = method == "getlist"
        if method in {"get", "getlist", "setdefault", "pop"}:
            self._add_request_field(base, field, is_list=is_list)

    def _add_request_field(self, base: str, field: str, is_list: bool) -> None:
        if base in self.request_aliases:
            collection = self.request_aliases[base]
            target = self.getlist if is_list else self.params
            target[collection].add(field)
            if collection == "headers" and field.lower() == "x-api-key":
                self.uses_webhook_key = True
            if field.lower() in {"csrf_token", "_csrf_token"}:
                self.uses_csrf_token = True
            return
        for collection in self.REQUEST_COLLECTIONS:
            if base == f"request.{collection}" or base.endswith(f".request.{collection}"):
                target = self.getlist if is_list else self.params
                target[collection].add(field)
                if collection == "headers" and field.lower() == "x-api-key":
                    self.uses_webhook_key = True
                if field.lower() in {"csrf_token", "_csrf_token"}:
                    self.uses_csrf_token = True

    def _request_collection_for_node(self, node: ast.AST | None) -> str | None:
        name = _call_name(node) if node else ""
        for collection in self.REQUEST_COLLECTIONS:
            if name == f"request.{collection}" or name.endswith(f".request.{collection}"):
                return collection
        if isinstance(node, ast.Name) and node.id in self.request_aliases:
            return self.request_aliases[node.id]
        return None

    def _capture_json_field_access(self, node: ast.Call, name: str) -> None:
        if _is_request_get_json(node):
            self.uses_json_request = True
            return
        if name.endswith(".get") and node.args:
            base = name.rsplit(".", 1)[0]
            field = _const_str(node.args[0])
            if field and base in self.json_vars:
                self.json_fields.add(field)

    def _add_json_field(self, name: str, field: str) -> None:
        if name in self.json_vars:
            self.json_fields.add(field)

    def _capture_response(self, node: ast.Call, name: str) -> None:
        short = name.rsplit(".", 1)[-1]
        if short == "jsonify":
            self.response_kinds.add("json")
        elif short == "render_template":
            self.response_kinds.add("html")
            if node.args:
                template = _const_str(node.args[0])
                if template:
                    self.templates.add(template)
        elif short == "redirect":
            self.response_kinds.add("redirect")
            self.redirects = True
            self.status_codes.add(302)
        elif short in {"send_file", "send_from_directory"}:
            self.response_kinds.add("file")
        elif short == "Response":
            self.response_kinds.add("raw")
        elif short == "abort" and node.args:
            code = _const_int(node.args[0])
            if code:
                self.status_codes.add(code)

    def visit_Return(self, node: ast.Return) -> Any:
        if isinstance(node.value, ast.Tuple) and len(node.value.elts) >= 2:
            code = _const_int(node.value.elts[1])
            if code:
                self.status_codes.add(code)
        self.generic_visit(node)


def _merge_analysis(target: RouteAnalyzer, source: RouteAnalyzer) -> None:
    for key in RouteAnalyzer.REQUEST_COLLECTIONS:
        target.params[key].update(source.params[key])
        target.getlist[key].update(source.getlist[key])
    target.json_fields.update(source.json_fields)
    target.json_vars.update(source.json_vars)
    target.status_codes.update(source.status_codes)
    target.response_kinds.update(source.response_kinds)
    target.templates.update(source.templates)
    target.redirects = target.redirects or source.redirects
    target.uses_json_request = target.uses_json_request or source.uses_json_request
    target.uses_webhook_key = target.uses_webhook_key or source.uses_webhook_key
    target.uses_csrf_token = target.uses_csrf_token or source.uses_csrf_token
    target.uses_subprocess = target.uses_subprocess or source.uses_subprocess


def _function_defs(module_tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in module_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _collection_for_arg(node: ast.AST, aliases: dict[str, str]) -> str | None:
    name = _call_name(node)
    for collection in RouteAnalyzer.REQUEST_COLLECTIONS:
        if name == f"request.{collection}" or name.endswith(f".request.{collection}"):
            return collection
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id]
    return None


def _analyse_called_helpers(
    owner: RouteAnalyzer,
    function_defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    visited: set[tuple[str, tuple[tuple[str, str], ...]]],
) -> None:
    for call in list(owner.helper_calls):
        if not isinstance(call.func, ast.Name):
            continue
        helper_name = call.func.id
        helper = function_defs.get(helper_name)
        if not helper:
            continue

        aliases: dict[str, str] = {}
        positional_params = [arg.arg for arg in helper.args.args]
        for idx, arg_node in enumerate(call.args):
            if idx >= len(positional_params):
                break
            collection = _collection_for_arg(arg_node, owner.request_aliases)
            if collection:
                aliases[positional_params[idx]] = collection
        for keyword in call.keywords:
            if not keyword.arg:
                continue
            collection = _collection_for_arg(keyword.value, owner.request_aliases)
            if collection:
                aliases[keyword.arg] = collection

        if not aliases:
            continue

        visit_key = (helper_name, tuple(sorted(aliases.items())))
        if visit_key in visited:
            continue
        visited.add(visit_key)

        helper_analyzer = RouteAnalyzer(request_aliases=aliases)
        helper_analyzer.visit(helper)
        _merge_analysis(owner, helper_analyzer)
        _analyse_called_helpers(helper_analyzer, function_defs, visited)
        _merge_analysis(owner, helper_analyzer)


def _analyse_function(func: Any) -> dict[str, Any]:
    try:
        source = inspect.getsource(func)
        file_path = inspect.getsourcefile(func) or ""
        line = inspect.getsourcelines(func)[1]
        tree = ast.parse(textwrap.dedent(source))
        module_tree = ast.parse(Path(file_path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "source_file": "",
            "source_line": None,
            "doc": inspect.getdoc(func) or "",
            "analysis_error": str(exc),
            "params": {},
            "getlist": {},
            "json_fields": [],
            "uses_json_request": False,
            "uses_webhook_key": False,
            "uses_csrf_token": False,
            "uses_subprocess": False,
            "status_codes": [],
            "response_kinds": [],
            "templates": [],
        }

    analyzer = RouteAnalyzer()
    analyzer.visit(tree)
    _analyse_called_helpers(analyzer, _function_defs(module_tree), set())
    return {
        "source_file": _rel(file_path),
        "source_line": line,
        "doc": inspect.getdoc(func) or "",
        "params": {k: sorted(v) for k, v in analyzer.params.items() if v},
        "getlist": {k: sorted(v) for k, v in analyzer.getlist.items() if v},
        "json_fields": sorted(analyzer.json_fields),
        "uses_json_request": analyzer.uses_json_request,
        "uses_webhook_key": analyzer.uses_webhook_key,
        "uses_csrf_token": analyzer.uses_csrf_token,
        "uses_subprocess": analyzer.uses_subprocess,
        "status_codes": sorted(analyzer.status_codes),
        "response_kinds": sorted(analyzer.response_kinds),
        "templates": sorted(analyzer.templates),
    }


def _openapi_path(flask_rule: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inside = match.group(1)
        if ":" in inside:
            _, name = inside.split(":", 1)
        else:
            name = inside
        return "{" + name + "}"

    return re.sub(r"<([^>]+)>", repl, flask_rule)


def _converter_types(rule: Any) -> dict[str, str]:
    mapping = {
        "IntegerConverter": "integer",
        "FloatConverter": "number",
        "PathConverter": "string",
        "UUIDConverter": "string",
        "UnicodeConverter": "string",
        "AnyConverter": "string",
    }
    out: dict[str, str] = {}
    for name, converter in getattr(rule, "_converters", {}).items():
        out[name] = mapping.get(type(converter).__name__, "string")
    return out


def _schema_for_fields(fields: list[str], list_fields: list[str] | None = None, files: bool = False) -> dict[str, Any]:
    list_set = set(list_fields or [])
    properties: dict[str, Any] = {}
    for field in sorted(set(fields) | list_set):
        if field in list_set:
            properties[field] = {"type": "array", "items": {"type": "string"}}
        elif files:
            properties[field] = {"type": "string", "format": "binary"}
        else:
            properties[field] = {"type": "string"}
    return {"type": "object", "additionalProperties": True, "properties": properties}


def _example_for_schema(schema: dict[str, Any]) -> dict[str, Any]:
    example: dict[str, Any] = {}
    for name, prop in schema.get("properties", {}).items():
        if prop.get("format") == "binary":
            example[name] = "<binary file>"
        elif prop.get("type") == "array":
            example[name] = ["example"]
        elif name.endswith("_id") or name in {"sid", "dec_id", "doc_id", "profile_id"}:
            example[name] = 123
        elif "date" in name:
            example[name] = "2026-05-06"
        else:
            example[name] = "example"
    return example


def _build_parameters(rule: Any, analysis: dict[str, Any]) -> list[dict[str, Any]]:
    converter_types = _converter_types(rule)
    params: list[dict[str, Any]] = []
    for name in sorted(rule.arguments):
        schema_type = converter_types.get(name, "string")
        param: dict[str, Any] = {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": schema_type},
            "description": f"Path parameter `{name}` from Flask route `{rule.rule}`.",
            "example": 123 if schema_type == "integer" else f"example-{name}",
        }
        if name in {"ens_ref", "cons_ref", "sfd_ref", "sup_ref", "gmr_ref", "goods_ref"}:
            param["example"] = name.replace("_ref", "").upper() + "0000000001"
        params.append(param)

    query_fields = set(analysis["params"].get("args", [])) | set(analysis["params"].get("values", []))
    query_list_fields = set(analysis["getlist"].get("args", [])) | set(analysis["getlist"].get("values", []))
    for name in sorted(query_fields | query_list_fields):
        is_list = name in query_list_fields
        params.append(
            {
                "name": name,
                "in": "query",
                "required": False,
                "schema": {"type": "array", "items": {"type": "string"}} if is_list else {"type": "string"},
                "style": "form",
                "explode": True,
                "description": "Inferred from request.args/request.values usage in route code.",
                "example": ["example"] if is_list else "example",
            }
        )

    for name in sorted(set(analysis["params"].get("headers", [])) | set(analysis["getlist"].get("headers", []))):
        params.append(
            {
                "name": name,
                "in": "header",
                "required": False,
                "schema": {"type": "string"},
                "description": "Inferred from request.headers usage in route code.",
                "example": "example",
            }
        )

    return params


def _build_request_body(analysis: dict[str, Any], methods: set[str]) -> dict[str, Any] | None:
    if not methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
        return None

    content: dict[str, Any] = {}
    form_fields = analysis["params"].get("form", [])
    form_list_fields = analysis["getlist"].get("form", [])
    files = analysis["params"].get("files", [])
    file_lists = analysis["getlist"].get("files", [])
    json_fields = analysis["json_fields"]

    if files or file_lists:
        schema = _schema_for_fields(form_fields + files, form_list_fields + file_lists, files=False)
        for f in files:
            schema["properties"][f] = {"type": "string", "format": "binary"}
        for f in file_lists:
            schema["properties"][f] = {"type": "array", "items": {"type": "string", "format": "binary"}}
        content["multipart/form-data"] = {
            "schema": schema,
            "example": _example_for_schema(schema),
        }

    if form_fields or form_list_fields:
        schema = _schema_for_fields(form_fields, form_list_fields)
        content["application/x-www-form-urlencoded"] = {
            "schema": schema,
            "example": _example_for_schema(schema),
        }

    if analysis["uses_json_request"] or json_fields:
        schema = _schema_for_fields(json_fields)
        content["application/json"] = {
            "schema": schema,
            "example": _example_for_schema(schema),
        }

    if not content and methods.intersection({"POST", "PUT", "PATCH"}):
        content["application/x-www-form-urlencoded"] = {
            "schema": {"type": "object", "additionalProperties": True},
            "example": {},
        }

    if not content:
        return None

    return {
        "required": False,
        "description": "Request body inferred from direct Flask request access in the route code.",
        "content": content,
    }


def _response_content(analysis: dict[str, Any]) -> dict[str, Any]:
    kinds = set(analysis["response_kinds"])
    if "json" in kinds:
        return {
            "application/json": {
                "schema": {"type": "object", "additionalProperties": True},
                "example": {"status": "ok"},
            }
        }
    if "file" in kinds:
        return {
            "application/octet-stream": {
                "schema": {"type": "string", "format": "binary"},
            }
        }
    if "raw" in kinds:
        return {
            "text/plain": {"schema": {"type": "string"}, "example": "response body"}
        }
    return {
        "text/html": {
            "schema": {"type": "string"},
            "example": "<html>...</html>",
        }
    }


def _build_responses(analysis: dict[str, Any], auth_mode: str, route_is_public: bool) -> dict[str, Any]:
    codes = set(analysis["status_codes"])
    if not codes:
        codes.add(200)
    if 302 in codes and 200 not in codes and "redirect" in analysis["response_kinds"]:
        # Many POST handlers only redirect after work; OpenAPI still benefits from a success code.
        codes.add(303)

    responses: dict[str, Any] = {}
    for code in sorted(codes):
        if code in {301, 302, 303, 307, 308}:
            responses[str(code)] = {
                "description": "Redirect response inferred from redirect() usage.",
                "headers": {"Location": {"schema": {"type": "string"}}},
            }
        else:
            responses[str(code)] = {
                "description": "Response inferred from route return statements and Flask helpers.",
                "content": _response_content(analysis),
            }

    if auth_mode == "portal" and not route_is_public:
        responses.setdefault(
            "302",
            {
                "description": "Unauthenticated portal requests are redirected to /auth/login by the global auth guard.",
                "headers": {"Location": {"schema": {"type": "string"}, "example": "/auth/login?next=/path"}},
            },
        )
    return responses


def _is_public_portal_route(rule: Any) -> bool:
    path = str(rule.rule)
    public = {"/auth/login", "/auth/logout", "/health", "/static"}
    return any(path == p or path.startswith(p + "/") for p in public)


def _security_for(app_meta: dict[str, Any], rule: Any, analysis: dict[str, Any]) -> list[dict[str, list[Any]]]:
    if app_meta["auth"] == "portal":
        if _is_public_portal_route(rule):
            return []
        return [{"cookieSession": []}]
    # The ingest service factory has no auth guard in code. X-API-Key appears only
    # where route code reads it explicitly.
    if analysis["uses_webhook_key"]:
        return [{"apiKeyAuth": []}]
    return []


def _operation_summary(rule: Any, analysis: dict[str, Any], method: str) -> str:
    doc = (analysis.get("doc") or "").strip().splitlines()
    if doc:
        return doc[0].rstrip(".")
    endpoint = str(rule.endpoint).replace(".", " ")
    return f"{method} {endpoint}".replace("_", " ").title()


def _operation_description(rule: Any, analysis: dict[str, Any]) -> str:
    parts: list[str] = []
    doc = (analysis.get("doc") or "").strip()
    if doc:
        parts.append(doc)
    source = analysis.get("source_file") or "unknown"
    if analysis.get("source_line"):
        source = f"{source}:{analysis.get('source_line')}"
    parts.append(f"Source: `{source}`")
    if analysis.get("templates"):
        parts.append("Templates rendered: " + ", ".join(f"`{t}`" for t in analysis["templates"]))
    if analysis.get("uses_subprocess"):
        parts.append("This route launches a subprocess according to the route code.")
    if analysis.get("analysis_error"):
        parts.append(f"Static analysis note: {analysis['analysis_error']}")
    return "\n\n".join(parts)


def _normalise_operation_id(service_slug: str, endpoint: str, method: str, path: str) -> str:
    raw = f"{service_slug}_{endpoint}_{method}_{path}".lower()
    return re.sub(r"[^a-z0-9_]+", "_", raw)


def _tag_for(rule: Any) -> str:
    endpoint = str(rule.endpoint)
    if "." in endpoint:
        return endpoint.split(".", 1)[0]
    if endpoint == "static":
        return "static"
    return "app"


def _build_spec(app_meta: dict[str, Any]) -> dict[str, Any]:
    app = app_meta["app"]
    paths: dict[str, Any] = defaultdict(dict)
    tags: set[str] = set()
    flask_rule_count = 0

    for rule in sorted(app.url_map.iter_rules(), key=lambda r: (r.rule, r.endpoint)):
        methods = set(rule.methods or set()) - STATIC_METHODS
        if not methods:
            continue
        flask_rule_count += len(methods)

        view_func = app.view_functions[rule.endpoint]
        analysis = _analyse_function(view_func)
        tag = _tag_for(rule)
        tags.add(tag)
        path = _openapi_path(rule.rule)

        for method in sorted(methods):
            operation = {
                "tags": [tag],
                "summary": _operation_summary(rule, analysis, method),
                "description": _operation_description(rule, analysis),
                "operationId": _normalise_operation_id(app_meta["slug"], rule.endpoint, method, path),
                "parameters": _build_parameters(rule, analysis),
                "responses": _build_responses(analysis, app_meta["auth"], _is_public_portal_route(rule)),
                "x-flask-endpoint": rule.endpoint,
                "x-source-file": analysis.get("source_file"),
                "x-source-line": analysis.get("source_line"),
                "x-code-analysis": {
                    "request_fields": analysis.get("params", {}),
                    "request_list_fields": analysis.get("getlist", {}),
                    "json_fields": analysis.get("json_fields", []),
                    "response_kinds": analysis.get("response_kinds", []),
                    "templates": analysis.get("templates", []),
                },
            }
            security = _security_for(app_meta, rule, analysis)
            if security:
                operation["security"] = security
            elif app_meta["auth"] == "portal" and _is_public_portal_route(rule):
                operation["security"] = []

            request_body = _build_request_body(analysis, {method})
            if request_body:
                operation["requestBody"] = request_body

            method_key = method.lower()
            if method_key in paths[path]:
                existing = paths[path][method_key]
                aliases = existing.setdefault("x-flask-aliases", [])
                alias_source = analysis.get("source_file") or "unknown"
                if analysis.get("source_line"):
                    alias_source = f"{alias_source}:{analysis.get('source_line')}"
                aliases.append(
                    {
                        "endpoint": rule.endpoint,
                        "source_file": analysis.get("source_file"),
                        "source_line": analysis.get("source_line"),
                        "summary": operation["summary"],
                    }
                )
                existing["description"] += (
                    "\n\nAdditional Flask route registered with the same path and method: "
                    f"`{rule.endpoint}` from `{alias_source}`."
                )
            else:
                paths[path][method_key] = operation

    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": f"{app_meta['name']} Internal API",
            "version": "1.0.0",
            "description": (
                app_meta["description"]
                + "\n\nGenerated from the real Flask url_map plus AST analysis of route functions."
            ),
        },
        "servers": [{"url": "/", "description": app_meta["name"]}],
        "tags": [{"name": name, "description": f"{name} blueprint/application routes."} for name in sorted(tags)],
        "components": {
            "securitySchemes": {
                "cookieSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "session",
                    "description": "Flask session cookie set by /auth/login in the portal.",
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "Optional webhook/API key header where route code checks X-API-Key.",
                },
            }
        },
        "paths": dict(paths),
        "x-generated-by": "scripts/generate_internal_openapi.py",
        "x-flask-factory": app_meta["factory"],
        "x-flask-rule-operation-count": flask_rule_count,
    }
    return spec


def _route_counts(spec: dict[str, Any]) -> tuple[int, int]:
    path_count = len(spec["paths"])
    operation_count = sum(len(methods) for methods in spec["paths"].values())
    return path_count, operation_count


def _write_summary(specs: list[tuple[dict[str, Any], Path]]) -> None:
    lines = [
        "# Internal OpenAPI Documentation",
        "",
        "Generated from the real Flask application URL maps and static analysis of `routes.py` functions.",
        "",
        "## Specs",
        "",
    ]
    for spec, path in specs:
        path_count, op_count = _route_counts(spec)
        alias_count = sum(
            len(operation.get("x-flask-aliases", []))
            for methods in spec["paths"].values()
            for operation in methods.values()
        )
        flask_count = spec.get("x-flask-rule-operation-count", op_count)
        alias_note = f", {alias_count} exact path/method alias" if alias_count == 1 else ""
        if alias_count > 1:
            alias_note = f", {alias_count} exact path/method aliases"
        lines.append(
            f"- `{_rel(path)}` - {path_count} paths, {op_count} OpenAPI operations "
            f"from {flask_count} Flask route-method registrations{alias_note}."
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Portal routes use the global Flask session guard except `/auth/login`, `/auth/logout`, `/health`, and static assets.",
            "- The standalone ingest service has no global auth guard in its Flask factory; endpoints that read `X-API-Key` are marked with `apiKeyAuth`.",
            "- Request fields are inferred from direct usage of `request.args`, `request.form`, `request.files`, `request.values`, `request.headers`, and `request.get_json()` in route code.",
            "- Helper functions may add extra validation or DB-driven fields that are not visible as direct `request.*` access in the route function; those gaps are preserved in `x-code-analysis` rather than guessed.",
            "",
        ]
    )
    (OUT_DIR / "INTERNAL_OPENAPI.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[tuple[dict[str, Any], Path]] = []
    for app_meta in _load_apps():
        spec = _build_spec(app_meta)
        out_path = OUT_DIR / f"{app_meta['slug']}.openapi.json"
        out_path.write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        written.append((spec, out_path))
        paths, operations = _route_counts(spec)
        print(f"Wrote {out_path.relative_to(ROOT)} ({paths} paths, {operations} operations)")

    _write_summary(written)
    print(f"Wrote {(OUT_DIR / 'INTERNAL_OPENAPI.md').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
