"""Search projection definitions for HQ record types."""

from __future__ import annotations

from application.resources import resource_search_definitions

DEFINITIONS = resource_search_definitions()

BY_MODEL = {definition.model: definition for definition in DEFINITIONS}
BY_SCOPE = {definition.scope: definition for definition in DEFINITIONS}
