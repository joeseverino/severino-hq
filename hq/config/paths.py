"""Local runtime defaults without silently abandoning an existing database."""

from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def database_path(root: Path, override: str | None) -> Path:
    if override is not None:
        return Path(override)
    if (root / "data" / "severino.sqlite3").exists():
        raise ImproperlyConfigured(
            "An existing database uses the old local default. Set "
            "SEVERINO_DATABASE_PATH to select it or a verified backup under var/db. "
            "See docs/REPOSITORY_LAYOUT.md."
        )
    return root / "var" / "db" / "severino.sqlite3"
