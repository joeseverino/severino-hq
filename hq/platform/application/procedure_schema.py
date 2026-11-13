"""The JSON Schema of a procedure's sidecar, written from ``ProcedureDeclaration``
so a repository of playbooks lints its sidecars against the contract HQ holds
them to. Where it is published and how a repository reads it:
docs/Architecture.md, "Declaring a playbook".
"""

import json
from pathlib import Path
from typing import Any

from .automation_sources import ProcedureDeclaration
from .json_schemas import PublishedSchema, json_schema

SCHEMA_PATH = Path(__file__).with_name("hq-procedure.schema.json")


def schema() -> dict[str, Any]:
    """The sidecar's JSON Schema, with its dialect, which a validator reads
    from the file since no API document carries it."""

    return {"$schema": PublishedSchema.schema_dialect, **json_schema(ProcedureDeclaration)}


def render() -> str:
    return json.dumps(schema(), indent=2, sort_keys=True) + "\n"
