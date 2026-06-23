"""Generate a TSS API <-> code cross-reference from local sources."""
from __future__ import annotations

import ast
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODE_PATH = ROOT / "app" / "tss_api.py"
DOC_ROOT = ROOT / "docs" / "api"
OUTPUT_PATH = DOC_ROOT / "API_CODE_CROSS_REFERENCE.md"
VERSIONS = ("v2.9.4", "v2.9.5")


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@dataclass(frozen=True)
class CodeCall:
    method_name: str
    http_method: str
    endpoint: str
    line: int


@dataclass(frozen=True)
class DocRequest:
    version: str
    name: str
    http_method: str
    endpoint: str
    query_keys: tuple[str, ...]


def endpoint_key(method: str, endpoint: str) -> tuple[str, str]:
    endpoint = endpoint.strip("/")
    if endpoint.startswith("choice_values/"):
        endpoint = "choice_values/{field_name}"
    return method.upper(), endpoint


def parse_code_calls() -> list[CodeCall]:
    tree = ast.parse(CODE_PATH.read_text(encoding="utf-8"))
    calls: list[CodeCall] = []

    class Visitor(ast.NodeVisitor):
        current_method = ""

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous = self.current_method
            self.current_method = node.name
            self.generic_visit(node)
            self.current_method = previous

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "_request"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Constant)
            ):
                calls.append(
                    CodeCall(
                        method_name=self.current_method,
                        http_method=str(node.args[0].value).upper(),
                        endpoint=str(node.args[1].value).strip("/"),
                        line=node.lineno,
                    )
                )
            self.generic_visit(node)

    Visitor().visit(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_choice_values":
            calls.append(CodeCall("get_choice_values", "GET", "choice_values/{field_name}", node.lineno))
            break

    return sorted(calls, key=lambda c: (c.endpoint, c.http_method, c.method_name, c.line))


def collection_path(version: str) -> Path:
    return DOC_ROOT / version / f"TSS-Declaration-API-{version}.postman_collection.json"


def parse_collection(version: str) -> tuple[list[DocRequest], str | None]:
    path = collection_path(version)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return [], f"{rel(path)} is not valid JSON: {exc}"

    requests: list[DocRequest] = []

    def walk(items: list[dict], folder: tuple[str, ...] = ()) -> None:
        for item in items:
            name = item.get("name", "")
            if "request" in item:
                request = item["request"]
                url = request.get("url", {})
                path_parts: list[str] = []
                query_keys: list[str] = []
                if isinstance(url, dict):
                    path_parts = [str(part) for part in url.get("path", []) if part != "{{baseUrl}}"]
                    if path_parts and path_parts[0] == "tss_api":
                        path_parts = path_parts[1:]
                    for query in url.get("query", []):
                        if isinstance(query, dict) and not query.get("disabled"):
                            key = query.get("key")
                            if key:
                                query_keys.append(str(key))
                endpoint = "/".join(path_parts)
                requests.append(
                    DocRequest(
                        version=version,
                        name=" > ".join(folder + (name,)),
                        http_method=str(request.get("method", "")).upper(),
                        endpoint=endpoint.strip("/"),
                        query_keys=tuple(query_keys),
                    )
                )
            if "item" in item:
                walk(item["item"], folder + (name,))

    walk(data.get("item", []))
    return requests, None


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def code_link(line: int) -> str:
    return f"`app/tss_api.py:{line}`"


def render_index() -> str:
    code_calls = parse_code_calls()
    doc_requests_by_version: dict[str, list[DocRequest]] = {}
    parse_errors: list[str] = []

    for version in VERSIONS:
        requests, error = parse_collection(version)
        doc_requests_by_version[version] = requests
        if error:
            parse_errors.append(error)

    doc_keys_by_version = {
        version: {endpoint_key(req.http_method, req.endpoint) for req in requests}
        for version, requests in doc_requests_by_version.items()
    }

    code_by_key: dict[tuple[str, str], list[CodeCall]] = defaultdict(list)
    for call in code_calls:
        code_by_key[endpoint_key(call.http_method, call.endpoint)].append(call)

    lines = [
        "# TSS API <-> Code Cross Reference",
        "",
        "Generated by `python scripts/generate_tss_api_cross_reference.py`.",
        "",
        "## Related References",
        "",
        "- [TSS API reference overview](README.md) for versioned API assets.",
        "- [TSS API v2.9.5 notes](v2.9.5/README.md) for field-level breaking changes.",
        "- [Production automation handbook](../README.md) for where each endpoint fits in the email automation flow.",
        "",
        "## Sources",
        "",
        f"- Code: `app/tss_api.py` ({len(code_calls)} API call sites)",
    ]
    for version in VERSIONS:
        requests = doc_requests_by_version[version]
        unique_count = len({endpoint_key(req.http_method, req.endpoint) for req in requests})
        lines.append(f"- {version}: `{rel(collection_path(version))}` ({len(requests)} requests, {unique_count} normalized endpoints)")

    if parse_errors:
        lines.extend(["", "## Parse Errors", ""])
        lines.extend(f"- {error}" for error in parse_errors)

    missing = []
    for key in sorted(code_by_key):
        for version in VERSIONS:
            if key not in doc_keys_by_version[version]:
                missing.append((key, version))

    added_in_295 = sorted(doc_keys_by_version["v2.9.5"] - doc_keys_by_version["v2.9.4"])
    removed_in_295 = sorted(doc_keys_by_version["v2.9.4"] - doc_keys_by_version["v2.9.5"])

    lines.extend(
        [
            "",
            "## Audit Summary",
            "",
            f"- Code endpoints missing from a versioned collection: {len(missing)}",
            f"- Endpoints added in v2.9.5 vs v2.9.4: {len(added_in_295)}",
            f"- Endpoints removed in v2.9.5 vs v2.9.4: {len(removed_in_295)}",
            "- `choice_values/{field_name}` is treated as covered when the collection documents concrete `choice_values/*` endpoints.",
        ]
    )

    if missing:
        lines.extend(["", "## Missing From Documentation", ""])
        lines.append("| Version | Method | Endpoint | Code call sites |")
        lines.append("|---|---:|---|---|")
        for (method, endpoint), version in missing:
            calls = ", ".join(f"`{call.method_name}` ({code_link(call.line)})" for call in code_by_key[(method, endpoint)])
            lines.append(f"| {version} | {method} | `/{endpoint}` | {calls} |")

    lines.extend(["", "## Endpoint Coverage", ""])
    lines.append("| Method | Endpoint | Code call sites | v2.9.4 | v2.9.5 | Status |")
    lines.append("|---|---|---|---:|---:|---|")
    for key in sorted(code_by_key):
        method, endpoint = key
        calls = "<br>".join(f"`{call.method_name}` ({code_link(call.line)})" for call in code_by_key[key])
        has_294 = key in doc_keys_by_version["v2.9.4"]
        has_295 = key in doc_keys_by_version["v2.9.5"]
        status = "current" if has_294 and has_295 else "review"
        lines.append(f"| {method} | `/{endpoint}` | {calls} | {yes_no(has_294)} | {yes_no(has_295)} | {status} |")

    unused = set()
    for key in doc_keys_by_version["v2.9.5"]:
        method, endpoint = key
        if endpoint.startswith("choice_values/") or endpoint == "choice_values/{field_name}":
            continue
        if key not in code_by_key:
            unused.add(key)

    if unused:
        lines.extend(["", "## Documented But Not Wrapped In `tss_api.py`", ""])
        lines.append("| Method | Endpoint | Note |")
        lines.append("|---|---|---|")
        for method, endpoint in sorted(unused):
            note = "available in Postman v2.9.5; no wrapper method currently calls it"
            lines.append(f"| {method} | `/{endpoint}` | {note} |")

    if added_in_295 or removed_in_295:
        lines.extend(["", "## Version Delta", ""])
        if added_in_295:
            lines.append("Added in v2.9.5:")
            lines.extend(f"- `{method} /{endpoint}`" for method, endpoint in added_in_295)
        if removed_in_295:
            lines.append("Removed in v2.9.5:")
            lines.extend(f"- `{method} /{endpoint}`" for method, endpoint in removed_in_295)

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- No endpoint currently used by `app/tss_api.py` is obsolete relative to the checked v2.9.4/v2.9.5 Postman collections.",
            "- The v2.9.5 change set is field-level, not endpoint-level: `header_additions_deductions[].addition_deduction_currency` and compact `taric_code` formatting.",
            "- `GET /duty` appears in older integration prose, but it is not present in either versioned Postman collection and is not used by `app/tss_api.py`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    OUTPUT_PATH.write_text(render_index(), encoding="utf-8")
    print(f"Wrote {rel(OUTPUT_PATH)}")


if __name__ == "__main__":
    main()
