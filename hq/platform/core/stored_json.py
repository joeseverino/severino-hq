"""A JSON column whose stored value HQ reads through a declared shape, and the
one check that loads every such value a database holds.

The column declares the function its value is read through, the one its
readers call, so the set of stored shapes is the set of these columns and a
release whose code refuses a value production already holds is caught on a
copy of production's data before it ships (``manage.py stored_json``).

Migrations see a plain ``JSONField``: a historical model reads its rows raw,
so a migration that rewrites a value the current shape refuses can read it.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.db import connection, models
from pydantic import ValidationError


class StoredJSONField(models.JSONField):
    """A JSON column read through ``load``, a function of its row."""

    def __init__(self, *args: Any, load: Callable[[Any], object], **kwargs: Any) -> None:
        self.load = load
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> Any:
        name, _path, args, kwargs = super().deconstruct()
        return name, "django.db.models.JSONField", args, kwargs

    def clone(self) -> StoredJSONField:
        _name, _path, args, kwargs = super().deconstruct()
        return type(self)(*args, load=self.load, **kwargs)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A stored value its column's shape refuses: where it is, and why, as
    the shape's error locations and types, never the value."""

    table: str
    row: str
    model: str
    field: str
    reasons: tuple[str, ...]

    def __str__(self) -> str:
        return f"refused {self.table}/{self.row}/{self.model}.{self.field} {' '.join(self.reasons)}"


def refusals() -> Iterator[Refusal]:
    """Every stored value of every installed model's ``StoredJSONField`` that
    its ``load`` refuses, read with the database held read-only."""

    with _read_only():
        for model in apps.get_models():
            fields = [field for field in model._meta.concrete_fields if isinstance(field, StoredJSONField)]
            for row in model._default_manager.order_by("pk").iterator() if fields else ():
                for field in fields:
                    try:
                        field.load(row)
                    except (ValueError, TypeError) as error:
                        yield Refusal(model._meta.db_table, str(row.pk), model._meta.label, field.name, _reasons(error))


def _reasons(error: Exception) -> tuple[str, ...]:
    if isinstance(error, ValidationError):
        return tuple(
            f"{'.'.join(str(part) for part in item['loc']) or 'value'}:{item['type']}"
            for item in error.errors(include_url=False)
        )
    return (type(error).__name__,)


@contextmanager
def _read_only() -> Iterator[None]:
    """SQLite's ``query_only`` for the check's reads, so no loader writes."""

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA query_only = ON")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA query_only = OFF")
