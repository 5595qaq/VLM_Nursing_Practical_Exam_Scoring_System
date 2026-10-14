"""Turn the full JSON Schema into one that Claude Structured Outputs accepts.

schema.json stays the single source of truth and is used as-is for full
validation (validate_result.py). Structured Outputs only supports a subset of
JSON Schema, so this module derives a copy without the unsupported keywords.
The removed constraints are still enforced by validate_result.py.

A schema may point at another file (actions.schema.json has
`"result": {"$ref": "schema.json"}`). Structured Outputs only resolves refs
inside the same document, so the CLI first copies each referenced file into
`$defs` (inline_file_refs) and then converts.

Usage:
    python scripts/schema_tools.py sop-video-skill/schema.json > schema.structured.json
    python scripts/schema_tools.py sop-video-skill/actions.schema.json > actions.structured.json
"""

import copy
import json
import sys
from pathlib import Path

# Keywords Structured Outputs rejects; numeric/string/array-size limits are
# checked afterwards in Python instead.
UNSUPPORTED_KEYWORDS = {
    "$schema",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
}

# Keywords whose value is a sub-schema, or a list/dict of sub-schemas.
_SUBSCHEMA = {"items", "not"}
_SUBSCHEMA_LIST = {"anyOf", "allOf", "oneOf", "prefixItems"}
_SUBSCHEMA_DICT = {"properties", "$defs", "definitions"}


def inline_file_refs(schema: dict, base_dir: Path) -> dict:
    """Return a copy of `schema` with every file `$ref` moved into `$defs`.

    `{"$ref": "schema.json"}` becomes `{"$ref": "#/$defs/schema"}` and the
    contents of base_dir/schema.json are stored under `$defs.schema`. Only one
    level is inlined: a referenced file must not itself point at other files.
    """
    schema = copy.deepcopy(schema)
    defs = schema.setdefault("$defs", {})

    def visit(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                name = Path(ref).name.split(".")[0]
                if name not in defs:
                    defs[name] = json.loads((base_dir / ref).read_text(encoding="utf-8"))
                node["$ref"] = f"#/$defs/{name}"
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit({k: v for k, v in schema.items() if k != "$defs"})
    for name in list(defs):
        visit(defs[name])
    if not defs:
        del schema["$defs"]
    return schema


def to_structured_output_schema(schema: dict) -> dict:
    """Return a copy of `schema` that Structured Outputs accepts.

    Raises ValueError if an object schema does not set
    `additionalProperties: false`, because Structured Outputs requires it.
    """
    return _convert(copy.deepcopy(schema), path="#")


def _convert(node: dict, path: str) -> dict:
    for key in UNSUPPORTED_KEYWORDS & node.keys():
        del node[key]

    ref = node.get("$ref")
    if isinstance(ref, str) and not ref.startswith("#"):
        raise ValueError(f"{path}: $ref {ref!r} points at a file; run inline_file_refs first")

    if "oneOf" in node:
        # Not supported; anyOf is the closest supported keyword.
        node["anyOf"] = node.pop("oneOf")

    if isinstance(node.get("type"), list):
        # "type": ["number", "null"] -> anyOf of single types (safer form).
        types = node.pop("type")
        node["anyOf"] = [{"type": t} for t in types]

    if node.get("type") == "object" and node.get("additionalProperties") is not False:
        raise ValueError(f"{path}: object schema must set additionalProperties: false")

    for key in _SUBSCHEMA & node.keys():
        node[key] = _convert(node[key], f"{path}/{key}")
    for key in _SUBSCHEMA_LIST & node.keys():
        node[key] = [_convert(s, f"{path}/{key}/{i}") for i, s in enumerate(node[key])]
    for key in _SUBSCHEMA_DICT & node.keys():
        node[key] = {name: _convert(s, f"{path}/{key}/{name}") for name, s in node[key].items()}
    return node


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[1])
    schema = inline_file_refs(json.loads(path.read_text(encoding="utf-8")), path.parent)
    sys.stdout.reconfigure(encoding="utf-8")  # schema text has non-ASCII (e.g. ≤)
    json.dump(to_structured_output_schema(schema), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
