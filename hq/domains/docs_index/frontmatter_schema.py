"""Canonical frontmatter schema, shared with severino-vault-mcp.

The MCP's ``schema.py`` is the single source of truth, emitted here as
``schema.json`` (regenerated, never hand-edited; the docs_index tests that
guard it name the command). The manifest importer validates against these
sets, so HQ and the MCP cannot disagree on what ``hq sync`` accepts.
"""

import json
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.json"

_data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

DOC_TYPES: frozenset[str] = frozenset(_data["doc_types"])
ENVIRONMENTS: frozenset[str] = frozenset(_data["environments"])
STATUSES: frozenset[str] = frozenset(_data["statuses"])
SENSITIVITIES: frozenset[str] = frozenset(_data["sensitivities"])
DOC_ID_PREFIXES: tuple[str, ...] = tuple(_data["doc_id_prefixes"])
REQUIRED_FIELDS: tuple[str, ...] = tuple(_data["required_fields"])

__all__ = [
    "DOC_ID_PREFIXES",
    "DOC_TYPES",
    "ENVIRONMENTS",
    "REQUIRED_FIELDS",
    "SCHEMA_PATH",
    "SENSITIVITIES",
    "STATUSES",
]
