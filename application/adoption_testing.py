"""Connections for tests that exercise adoption."""

from __future__ import annotations

from django.utils import timezone

from control_plane.models import ProviderConnection
from control_plane.providers import CONNECTION_CREDENTIALS


def connection(
    provider: str, connection_ref: str = "", *, manages: bool = True
) -> ProviderConnection:
    """One reported connection of ``provider``; managing unless told otherwise."""

    found, _ = ProviderConnection.objects.update_or_create(
        connection_ref=connection_ref or f"example-{provider}",
        controller_id="",
        defaults={"provider": provider, "manages": manages, "observed_at": timezone.now()},
    )
    return found


def managing_everything(*refs: tuple[str, str]) -> None:
    """A managing connection per provider, plus each named ``(provider, ref)``."""

    for provider in CONNECTION_CREDENTIALS:
        connection(provider)
    for provider, ref in refs:
        connection(provider, ref)
