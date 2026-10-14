"""Tests for schema_tools.py (full schema -> Structured Outputs schema)."""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from schema_tools import UNSUPPORTED_KEYWORDS, inline_file_refs, to_structured_output_schema
from validate_result import load_schema


def walk(node):
    """Yield every sub-schema dict in a schema tree."""
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key in ("properties", "$defs"):
                for sub in value.values():
                    yield from walk(sub)
            elif isinstance(value, (dict, list)):
                yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_schema_json_is_a_valid_json_schema():
    Draft202012Validator.check_schema(load_schema())


def test_every_object_in_schema_json_is_closed():
    for node in walk(load_schema()):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node


def test_converted_schema_has_no_unsupported_keywords():
    converted = to_structured_output_schema(load_schema())
    for node in walk(converted):
        assert not (UNSUPPORTED_KEYWORDS & node.keys()), node
        assert "oneOf" not in node
        assert not isinstance(node.get("type"), list), node


def test_converted_schema_is_still_valid_json_schema():
    Draft202012Validator.check_schema(to_structured_output_schema(load_schema()))


def test_conversion_does_not_change_the_original():
    schema = load_schema()
    before = copy.deepcopy(schema)
    to_structured_output_schema(schema)
    assert schema == before


def test_type_list_becomes_any_of():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"start": {"type": ["number", "null"]}}}
    converted = to_structured_output_schema(schema)
    assert converted["properties"]["start"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}


def test_one_of_becomes_any_of():
    schema = {"oneOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]}
    assert to_structured_output_schema(schema) == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_property_named_like_a_keyword_is_kept():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"minimum": {"type": "number"}}}
    converted = to_structured_output_schema(schema)
    assert "minimum" in converted["properties"]


def test_open_object_is_rejected():
    schema = {"type": "object", "properties": {"inner": {"type": "object"}},
              "additionalProperties": False}
    with pytest.raises(ValueError, match="#/properties/inner"):
        to_structured_output_schema(schema)


# --- actions.schema.json (final.result points at schema.json) ----------------

SKILL_DIR = Path(__file__).resolve().parents[2] / "sop-video-skill"


def load_actions_schema() -> dict:
    return json.loads((SKILL_DIR / "actions.schema.json").read_text(encoding="utf-8"))


def test_actions_schema_converts_after_inlining():
    bundled = inline_file_refs(load_actions_schema(), SKILL_DIR)
    converted = to_structured_output_schema(bundled)
    Draft202012Validator.check_schema(converted)
    final = next(b for b in converted["anyOf"] if b["properties"]["action"].get("const") == "final")
    assert final["properties"]["result"]["$ref"] == "#/$defs/schema"
    assert converted["$defs"]["schema"]["required"] == load_schema()["required"]


def test_file_ref_without_inlining_is_rejected():
    with pytest.raises(ValueError, match="run inline_file_refs first"):
        to_structured_output_schema(load_actions_schema())


def test_inline_file_refs_does_not_change_the_original():
    schema = load_actions_schema()
    before = copy.deepcopy(schema)
    inline_file_refs(schema, SKILL_DIR)
    assert schema == before
