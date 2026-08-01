#!/usr/bin/env python3
"""Generate ``apps/web/src/api/schema.d.ts`` from OpenAPI (task 9.12).

    make schema

**Never hand-edited.** ``test_schema_conformance.py`` regenerates and compares,
so an edit to the output file is a test failure rather than a convention nobody
enforces — and a server-side rename that drifts from the client's types fails the
build instead of surfacing as a blank card on demo day.

### Why this is not ``openapi-typescript``

That is the standard tool and it was the first choice. Every published version
(≤7.13.0) declares ``peerDependencies: {"typescript": "^5.x"}`` and this project
is on TypeScript 6, so ``npm install`` refuses. The workaround is
``--legacy-peer-deps``, which writes a resolution into the lockfile that papers
over a real declared conflict and tends to break on someone else's fresh
``npm ci``.

Emitting the subset ourselves costs about a hundred lines and buys two things
worth more than the dependency:

* ``make schema`` and the drift test need **only Python** — no node, no network,
  no lockfile surgery. They run wherever the rest of the suite runs.
* The test can regenerate and byte-compare. With ``npx`` it could only parse the
  committed file, which cannot tell a stale file from a hand-edited one.

The subset is narrow on purpose: our schemas are flat objects, arrays, unions
with null, and refs. Anything outside that emits ``unknown`` rather than
guessing, so an unsupported construct shows up as a useless type rather than as
a wrong one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = REPO_ROOT / "apps" / "web" / "src" / "api" / "schema.d.ts"

_PRIMITIVES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
}


def type_of(schema: dict[str, Any]) -> str:
    """One JSON Schema node as a TypeScript type expression."""
    if ref := schema.get("$ref"):
        return f'components["schemas"]["{ref.rsplit("/", 1)[-1]}"]'

    if any_of := schema.get("anyOf") or schema.get("oneOf"):
        parts = [type_of(option) for option in any_of]
        # `null` last reads the way an optional field is thought about: the value
        # first, its absence after.
        parts = [part for part in parts if part != "null"] + (
            ["null"] if "null" in parts else []
        )
        return " | ".join(dict.fromkeys(parts))

    if enum := schema.get("enum"):
        return " | ".join(_literal(value) for value in enum)

    kind = schema.get("type")
    if kind == "null":
        return "null"
    if kind == "array":
        item = type_of(schema.get("items", {}))
        # Parenthesise a union so `A | B[]` cannot be read for `(A | B)[]`.
        return f"({item})[]" if "|" in item else f"{item}[]"
    if kind == "object" or (kind is None and "additionalProperties" in schema):
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict) and extra:
            return f"Record<string, {type_of(extra)}>"
        if schema.get("properties"):
            return _object(schema, indent="  ")
        return "Record<string, unknown>"
    if isinstance(kind, str) and kind in _PRIMITIVES:
        return _PRIMITIVES[kind]
    # No type at all — a bare `{}` in OpenAPI. `unknown` rather than `any`, so a
    # consumer has to narrow it instead of silently trusting it.
    return "unknown"


def _literal(value: Any) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def _object(schema: dict[str, Any], *, indent: str) -> str:
    required = set(schema.get("required", []))
    lines = ["{"]
    for name, prop in schema.get("properties", {}).items():
        optional = "" if name in required else "?"
        if description := (prop.get("description") or "").strip():
            first = description.splitlines()[0]
            lines.append(f"{indent}  /** {first} */")
        lines.append(f"{indent}  {name}{optional}: {type_of(prop)};")
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def render(spec: dict[str, Any]) -> str:
    schemas = spec.get("components", {}).get("schemas", {})
    out = [
        "/**",
        " * Generated from the API's OpenAPI schema. DO NOT EDIT.",
        " *",
        " * Regenerate with `make schema`. A hand edit fails",
        " * `tests/test_schema_conformance.py`, which regenerates and compares —",
        " * because the point of generating these is that the client's idea of a",
        " * shape and the server's cannot drift apart silently.",
        " */",
        "",
        "export interface components {",
        "  schemas: {",
    ]
    for name in sorted(schemas):
        schema = schemas[name]
        # Not everything named is an object: an enum used as a request field
        # (`ProviderKind`) is published as a string with an `enum`, and running
        # it through the object emitter produced a bare `{}` — a type that
        # accepts anything, which is worse than no type because it looks like one.
        body = (
            _object(schema, indent="    ")
            if schema.get("properties")
            else type_of(schema)
        )
        out.append(f"    {name}: {body};")
    out += ["  };", "}", ""]

    # The two shapes the brief closes, re-exported by name. Everything else is
    # reached through `components["schemas"]`; these two are what the console
    # actually holds in its hands, and they are the ones under contract.
    for name in ("Result", "Draft"):
        if name in schemas:
            out.append(f'export type {name} = components["schemas"]["{name}"];')
    out.append("")
    return "\n".join(out)


def openapi() -> dict[str, Any]:
    """The live schema, straight from the app.

    Built in-process rather than fetched over HTTP: a generator that needs a
    running server is a generator that silently regenerates against whatever
    happened to be on port 8080 — which, on this machine, has twice been a stale
    container serving a previous build.
    """
    from api.main import create_app

    spec: dict[str, Any] = create_app(run_worker_inline=False).openapi()
    return spec


def main() -> int:
    rendered = render(openapi())
    previous = OUTPUT.read_text() if OUTPUT.exists() else ""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered)

    if rendered == previous:
        print(f"{OUTPUT.relative_to(REPO_ROOT)} is up to date")
        return 0
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(rendered.splitlines())} lines)")
    if previous:
        # A regeneration that changes a shape the console renders is worth
        # reading before committing, and `git diff` says it better than anything
        # this script could print.
        print("  the shapes moved — read `git diff` before committing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
