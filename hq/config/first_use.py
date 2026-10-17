"""Defer importing some third-party packages until their first use (PEP 810).

Most of what the URLconf and settings pull in serves one path only: the MCP
stack for /mcp, requests and PyJWT for sign-in, sqlparse for migrations.

Only the packages named here are deferred. HQ's own modules, Django and the
extensions register things when imported, and deferring them would silently
skip that.

Call first in each entry point (`manage.py`, `hq.config.asgi`). Not through
`PYTHON_LAZY_IMPORTS`: that makes every import lazy before this filter exists.
"""

import sys

FIRST_USE = frozenset(
    {
        # The MCP server and what it serves with.
        "mcp",
        "mcp_types",
        "httpx2",
        "httpcore2",
        "sse_starlette",
        "python_multipart",
        "jsonschema",
        "referencing",
        "rpds",
        "attr",
        "attrs",
        "opentelemetry",
        # Sign-in and bearer tokens.
        "requests",
        "urllib3",
        "charset_normalizer",
        "idna",
        "certifi",
        "jwt",
        "cryptography",
        # Migrations and the database shell.
        "sqlparse",
    }
)


def _deferred(importer: str, name: str, fromlist: tuple[str, ...] | None) -> bool:
    del importer, fromlist
    return name.partition(".")[0] in FIRST_USE


def load_on_first_use() -> None:
    """Defer the packages in FIRST_USE, and nothing else, from here on."""

    sys.set_lazy_imports_filter(_deferred)
    sys.set_lazy_imports("all")
