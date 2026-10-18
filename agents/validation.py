"""Local JSON Schema validation without fetching external references."""
from __future__ import annotations

import json
from jsonschema import Draft202012Validator
from referencing import Registry


def validate_schema(schema) -> str | None:
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return f"invalid JSON Schema: {type(exc).__name__}"
    return None


def validate_json(value, schema: dict) -> str | None:
    try:
        json.dumps(value, allow_nan=False)
        Draft202012Validator.check_schema(schema)
        errors = sorted(Draft202012Validator(schema, registry=Registry()).iter_errors(value),
                        key=lambda error: str(list(error.path)))
        if errors:
            error = errors[0]
            path = ".".join(map(str, error.path)) or "input"
            return f"{path}: {error.message}"
    except Exception as exc:
        return f"schema validation failed: {type(exc).__name__}"
    return None
