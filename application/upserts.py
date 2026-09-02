"""Shared transaction boundary for command-slug upserts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from django.db import models, transaction

from .security import Principal


class SlugCommand(Protocol):
    slug: str


Command = TypeVar("Command", bound=SlugCommand)


@transaction.atomic
def upsert_by_slug(
    model: type[models.Model],
    command: Command,
    save: Callable[..., dict[str, Any]],
    *,
    principal: Principal,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    """Lock an existing command slug, then delegate the domain-specific write."""

    exists = (
        model._default_manager.select_for_update().filter(slug=command.slug).exists()
    )
    return save(
        command,
        principal=principal,
        current_slug=command.slug if exists else None,
        expected_updated_at=expected_updated_at,
    )
