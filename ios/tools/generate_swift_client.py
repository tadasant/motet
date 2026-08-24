"""Generate the iOS client's Swift types from ``openapi.yaml``.

The SPA's TypeScript client is generated from the contract so the two can never drift
(`bin/generate-client`). The app gets the same guarantee from this script, and `bin/ci`
regenerates and diffs both. The alternative — hand-written request code on iOS — is the
one client that would silently keep compiling after the contract moved, and it is also the
client that ships through App Store review, so drift there is the most expensive kind.

`openapi-generator`/`swift-openapi-generator` were the obvious alternative. Both want a
Swift toolchain (or a JVM) at generation time, and `bin/ci` has to run offline on a laptop
with neither; this script is ~400 lines of stdlib Python that emits the same shapes.

What it emits, from ``components.schemas`` and ``paths``:

* one ``Codable`` struct per schema, camelCased with ``CodingKeys`` back to the wire names;
* one ``HTTPEndpoint`` factory per operation, with path and query parameters as arguments.

What it deliberately does not emit:

* **header parameters.** FastAPI declares ``authorization`` on every ``/v1`` operation, but
  authentication is one concern applied centrally by ``MotetHTTPClient`` — threading a
  bearer token through 9 generated signatures would invite one call site to pass the wrong
  one.
* **response wiring.** Which type an operation returns is stated once, in ``MotetAPI``'s
  implementation, where the error mapping lives too.
"""

from __future__ import annotations

import keyword
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = REPO_ROOT / "openapi.yaml"
OUTPUT_PATH = REPO_ROOT / "ios" / "Sources" / "MotetKit" / "Generated" / "Schema.swift"

HEADER = """\
// Generated from openapi.yaml by ios/tools/generate_swift_client.py — do not edit by hand.
//
// Regenerate with `bin/generate-ios-client`. `bin/ci` regenerates it and fails on any
// diff, so this file, openapi.yaml, and the FastAPI app cannot drift apart.

import Foundation
"""

#: Swift keywords that cannot be used bare as property names.
SWIFT_RESERVED = {
    "associatedtype",
    "class",
    "deinit",
    "enum",
    "extension",
    "fileprivate",
    "func",
    "import",
    "init",
    "inout",
    "internal",
    "let",
    "operator",
    "private",
    "protocol",
    "public",
    "static",
    "struct",
    "subscript",
    "typealias",
    "var",
    "break",
    "case",
    "continue",
    "default",
    "defer",
    "do",
    "else",
    "fallthrough",
    "for",
    "guard",
    "if",
    "in",
    "repeat",
    "return",
    "switch",
    "where",
    "while",
    "as",
    "catch",
    "false",
    "is",
    "nil",
    "rethrows",
    "super",
    "self",
    "Self",
    "throw",
    "throws",
    "true",
    "try",
    "Any",
    "Protocol",
    "Type",
}

#: Operations the app has no use for are still generated: an endpoint factory costs two
#: lines and its absence is what makes someone hand-write a URL. Only these HTTP methods
#: are recognised, so a spec gaining `patch` fails loudly rather than emitting nothing.
HTTP_METHODS = ("get", "post", "put", "patch", "delete")


class GenerationError(RuntimeError):
    """The spec contains something this generator cannot express in Swift."""


def camel_case(name: str, *, upper: bool = False) -> str:
    """``source_item_id`` -> ``sourceItemId``; ``feed.xml`` -> ``feedXml``."""
    normalised = name.replace("-", "_").replace(".", "_").replace("/", "_").replace(" ", "_")
    parts = [p for p in normalised.split("_") if p]
    if not parts:
        raise GenerationError(f"cannot derive a Swift identifier from {name!r}")
    head = parts[0]
    first = head[:1].upper() + head[1:] if upper else head[:1].lower() + head[1:]
    return first + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def swift_property_name(wire_name: str) -> str:
    name = camel_case(wire_name)
    if name in SWIFT_RESERVED or keyword.iskeyword(name):
        return f"`{name}`"
    return name


def ref_name(ref: str) -> str:
    if not ref.startswith("#/components/schemas/"):
        raise GenerationError(f"only local schema refs are supported, got {ref!r}")
    return ref.rsplit("/", 1)[-1]


def swift_type(schema: dict[str, Any]) -> tuple[str, bool]:
    """Map one JSON Schema node to ``(swift type, is optional)``.

    ``anyOf: [T, null]`` — how FastAPI spells ``T | None`` — collapses to Swift's ``T?``.
    A union of two *real* primitives (``ValidationError.loc`` is ``string | integer``)
    becomes ``JSONValue``, because Swift has no untagged union and the alternative is
    inventing an enum for an error-detail field nobody reads field by field. A union
    involving a *modelled* type is a different matter and raises: that is a contract shape
    the app would want to switch on, and quietly flattening it to JSON would hide it.
    """
    if "$ref" in schema:
        return ref_name(schema["$ref"]), False

    if "anyOf" in schema:
        variants = schema["anyOf"]
        non_null = [v for v in variants if v.get("type") != "null"]
        nullable = len(non_null) < len(variants)
        if len(non_null) != 1:
            if any("$ref" in v or v.get("type") in ("array", "object") for v in non_null):
                raise GenerationError(f"unsupported anyOf over modelled types: {non_null!r}")
            return "JSONValue", nullable
        inner, _ = swift_type(non_null[0])
        return inner, nullable

    type_name = schema.get("type")
    if type_name == "string":
        return ("Date" if schema.get("format") == "date-time" else "String"), False
    if type_name == "integer":
        return "Int", False
    if type_name == "number":
        return "Double", False
    if type_name == "boolean":
        return "Bool", False
    if type_name == "array":
        items = schema.get("items")
        if items is None:
            return "[JSONValue]", False
        inner, inner_optional = swift_type(items)
        return f"[{inner}{'?' if inner_optional else ''}]", False
    if type_name == "object" or type_name is None:
        # `ValidationError.ctx` is a bare `object` and `.input` has no type at all. Both
        # are FastAPI's 422 detail, which the client surfaces as text rather than reads
        # field by field, so an any-JSON value is the honest mapping.
        return "JSONValue", False
    raise GenerationError(f"unsupported schema type {type_name!r}")


def render_struct(name: str, schema: dict[str, Any]) -> str:
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields: list[tuple[str, str, str, bool]] = []  # (wire, swift name, type, optional)
    for wire_name in properties:
        base_type, nullable = swift_type(properties[wire_name])
        optional = nullable or wire_name not in required
        fields.append((wire_name, swift_property_name(wire_name), base_type, optional))

    lines: list[str] = []
    description = schema.get("description")
    if description:
        lines.extend(f"/// {line}".rstrip() for line in description.strip().splitlines())
    lines.append(f"public struct {name}: Codable, Hashable, Sendable {{")
    for _, prop, type_name, optional in fields:
        lines.append(f"    public var {prop}: {type_name}{'?' if optional else ''}")

    if fields:
        lines.append("")
        args = [
            f"{prop}: {type_name}{'? = nil' if optional else ''}"
            for _, prop, type_name, optional in fields
        ]
        one_line = f"    public init({', '.join(args)}) {{"
        if len(one_line) <= 100:
            lines.append(one_line)
        else:
            lines.append("    public init(")
            lines.extend(f"        {arg}," for arg in args[:-1])
            lines.append(f"        {args[-1]}")
            lines.append("    ) {")
        for _, prop, _, _ in fields:
            bare = prop.strip("`")
            lines.append(f"        self.{prop} = {bare}")
        lines.append("    }")

    if any(wire != prop.strip("`") for wire, prop, _, _ in fields):
        lines.append("")
        lines.append("    private enum CodingKeys: String, CodingKey {")
        for wire, prop, _, _ in fields:
            bare = prop.strip("`")
            if wire == bare:
                lines.append(f"        case {prop}")
            else:
                lines.append(f'        case {prop} = "{wire}"')
        lines.append("    }")

    lines.append("}")
    return "\n".join(lines)


def _parameters(operation: dict[str, Any], path_item: dict[str, Any]) -> list[dict[str, Any]]:
    return list(path_item.get("parameters", [])) + list(operation.get("parameters", []))


def render_endpoint(
    path: str, method: str, operation: dict[str, Any], shared: dict[str, Any]
) -> str:
    params = _parameters(operation, shared)
    path_params = [p for p in params if p.get("in") == "path"]
    query_params = [p for p in params if p.get("in") == "query"]

    func_name = camel_case(operation.get("operationId") or f"{method}_{path}")
    # FastAPI suffixes every operationId with its route and verb
    # (`get_episode_v1_episodes__episode_id__get`). Keep the leading summary words only —
    # the rest is the path, which is right there in the returned endpoint.
    summary = operation.get("summary")
    if summary:
        func_name = camel_case(summary)

    args: list[str] = []
    swift_path = path
    for param in path_params:
        arg = swift_property_name(param["name"])
        args.append(f"{arg}: String")
        swift_path = swift_path.replace(
            "{" + param["name"] + "}", f"\\(MotetPathComponent({arg.strip('`')}))"
        )
    for param in query_params:
        arg = swift_property_name(param["name"])
        type_name, _ = swift_type(param.get("schema", {"type": "string"}))
        args.append(f"{arg}: {type_name}? = nil")

    lines: list[str] = []
    lines.append(f"    /// `{method.upper()} {path}`" + (f" — {summary}" if summary else ""))
    if args:
        signature = f"    public static func {func_name}({', '.join(args)}) -> HTTPEndpoint"
    else:
        signature = f"    public static var {func_name}: HTTPEndpoint"
    lines.append(signature + " {")

    if query_params:
        lines.append("        var query: [String: String] = [:]")
        for param in query_params:
            arg = swift_property_name(param["name"]).strip("`")
            wire = param["name"]
            lines.append(f'        if let {arg} {{ query["{wire}"] = String(describing: {arg}) }}')
        query_arg = ", query: query"
    else:
        query_arg = ""

    lines.append(
        f'        return HTTPEndpoint(method: "{method.upper()}", path: "{swift_path}"{query_arg})'
    )
    lines.append("    }")
    return "\n".join(lines)


def generate(spec: dict[str, Any]) -> str:
    chunks: list[str] = [HEADER]

    chunks.append("// MARK: - Schemas\n")
    schemas: dict[str, Any] = spec.get("components", {}).get("schemas", {})
    for name in sorted(schemas):
        chunks.append(render_struct(name, schemas[name]) + "\n")

    chunks.append("// MARK: - Endpoints\n")
    chunks.append(
        "/// Every operation in the contract, as a method, a path, and its query.\n"
        "///\n"
        "/// Header parameters are absent on purpose: `authorization` is applied centrally by\n"
        "/// `MotetHTTPClient`, so no call site can pass the wrong token.\n"
        "public enum MotetEndpoints {"
    )
    endpoints: list[str] = []
    paths: dict[str, Any] = spec.get("paths", {})
    for path in sorted(paths):
        path_item = paths[path]
        for method in HTTP_METHODS:
            if method in path_item:
                endpoints.append(render_endpoint(path, method, path_item[method], path_item))
    chunks.append("\n\n".join(endpoints))
    chunks.append("}\n")

    return "\n".join(chunks)


def main(argv: list[str]) -> int:
    spec = yaml.safe_load(SPEC_PATH.read_text())
    rendered = generate(spec)
    check_only = "--check" in argv
    if check_only:
        current = OUTPUT_PATH.read_text() if OUTPUT_PATH.exists() else ""
        return 0 if current == rendered else 1
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
