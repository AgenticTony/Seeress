"""Reading a schema of a contract, and asking whether it accepts an instance.

Shared by every file of the contract tests, so that none imports from another.
"""

import json
from pathlib import Path
from typing import Any

import jsonschema

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def schema(name: str) -> dict[str, Any]:
    """The schema in the named file, checked first to be a schema at all."""
    document: dict[str, Any] = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(document)
    return document


def accepted(document: dict[str, Any], instance: object) -> bool:
    """Whether the schema accepts the instance."""
    return bool(jsonschema.Draft202012Validator(document).is_valid(instance))
