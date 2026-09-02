"""Leaf contracts shared by integration emitters and the graph compiler."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .search_contracts import SearchDefinition
from .security import Capability


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    summary: str
    effect: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    command_type: type
    handler: Callable
    target_kind: str | None = None
    subject_resource: str | None = None
    target_label: str = ""
    target_help: str = ""
    target_query: tuple[tuple[str, str | int | float | bool], ...] = ()
    execution_notes: tuple[str, ...] = ()
    target_initial_fields: tuple[str, ...] = ()

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)


@dataclass(frozen=True)
class ResourceSpec:
    """One declaration of a readable domain and every operation it supports."""

    name: str
    label: str
    summary: str
    required_capability: Capability | str | tuple[Capability | str, ...]
    list_handler: Callable[..., dict[str, Any]] | None = None
    list_query_type: type[BaseModel] | None = None
    detail_handler: Callable[[Any], dict[str, Any]] | None = None
    identifier: str | None = None
    identifier_type: type = str
    not_found_errors: tuple[type[Exception], ...] = ()
    search: SearchDefinition | None = None
    web_route: str = ""

    @property
    def required_capabilities(self) -> tuple[Capability | str, ...]:
        if isinstance(self.required_capability, tuple):
            return self.required_capability
        return (self.required_capability,)
