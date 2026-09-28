"""What the controller registers against a kind: its readers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


# ----- Readings ---------------------------------------------------------------
#
# A reader for a kind in control_plane.observations is registered one of two
# ways: an integration's adapter declares it (``readings=``) and
# ``_register_adapter_readings`` admits it, or a core reader here carries
# @reads beside its own definition. ``test_reader_registration`` holds it so.

OBSERVATION_READERS: dict[str, Callable[[], list[dict[str, Any]]]] = {}


def reads(kind: str):
    def register(reader):
        if kind in OBSERVATION_READERS:
            raise ValueError(f"Duplicate reader for {kind!r}.")
        OBSERVATION_READERS[kind] = reader
        return reader

    return register
