"""Canonical frontmatter schema, shared with severino-vault-mcp.

The MCP's ``schema.py`` is the single source of truth for the vault frontmatter
contract. It emits that contract as JSON (``severino-vault-mcp schema --json``),
which is committed here as ``schema.json``. The manifest importer validates
against these sets, so HQ and the MCP cannot disagree on what ``hq sync``
accepts.

Do not hand-edit ``schema.json`` — regenerate it:

    severino-vault-mcp schema --json > docs_index/schema.json

Two guards keep it honest (see ``test_schema_contract.py``): one asserts the
committed JSON still matches the installed MCP, the other asserts the
``DocumentationRecord`` choice members still cover these sets.
"""

from __future__ import annotations

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
