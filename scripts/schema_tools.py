"""Turn the full JSON Schema into one that Claude Structured Outputs accepts.

schema.json stays the single source of truth and is used as-is for full
validation (validate_result.py). Structured Outputs only supports a subset of
JSON Schema, so this module derives a copy without the unsupported keywords.
The removed constraints are still enforced by validate_result.py.

Usage:
    python scripts/schema_tools.py schema.json > schema.structured.json
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


def to_structured_output_schema(schema: dict) -> dict:
    """Return a copy of `schema` that Structured Outputs accepts.

    Raises ValueError if an object schema does not set
    `additionalProperties: false`, because Structured Outputs requires it.
    """
    return _convert(copy.deepcopy(schema), path="#")


def _convert(node: dict, path: str) -> dict:
    for key in UNSUPPORTED_KEYWORDS & node.keys():
        del node[key]

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
    schema = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    json.dump(to_structured_output_schema(schema), sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
