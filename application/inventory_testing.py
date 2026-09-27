"""A stored sweep for tests that read what the controller reported."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from control_plane.models import ProviderInventory


def store(kind: str, *records: Any, age: timedelta = timedelta(0), **fields: Any) -> None:
    """One kind as a connected, reachable sweep reported it ``age`` ago.

    ``fields`` override the stored snapshot's own: ``reachable``, ``error``,
    ``refused_parts`` and the rest.
    """

    ProviderInventory.objects.update_or_create(
        kind=kind,
        defaults={
            "records": list(records),
            "reachable": True,
            "connected": True,
            "observed_at": timezone.now() - age,
            "controller_id": "example-controller",
            **fields,
        },
    )
