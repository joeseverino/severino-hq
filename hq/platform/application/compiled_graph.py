"""The integration graph a composition compiles at a catalog generation.

Only the command registry (``capabilities``) reads it at the catalog adopted
now, the one read that asks the database; everything else reads
``integrations.held_graph``. lint:imports holds every other importer out.
"""

from contextvars import ContextVar

from .integration_specs import command_schema
from .integrations import IntegrationGraph, IntegrationGraphError, IntegrationViolation, compile_integration_graph
from .json_schemas import fields
from .process_caches import kept
lazy from . import automation_adoption, automation_runs, outbound_work, plugins
lazy from .domains import host_specs
lazy from .plugins import plugin_capability_specs, plugin_search_definitions
lazy from .records import capability_specs as record_capability_specs


# One graph at a time, keyed by the composition and the adopted catalog's
# digest: either moves the commands, and the graph is built anew at its next ask.
@kept
def compiled(_composition: plugins.Composition, generation: str) -> IntegrationGraph:
    host = (*record_capability_specs(), *host_specs("capabilities"))
    # HQ's own command says what it is in words of its own; only an
    # extension's falls back to the words of its name.
    if unnamed := sorted(spec.name for spec in host if not spec.label):
        raise IntegrationGraphError([IntegrationViolation("unlabeled_command", f"Give {', '.join(unnamed)} a label.")])
    fixed = (*host, *plugin_capability_specs(), *outbound_work.capability_specs())
    # Only the procedures move with an adoption, which is what lets
    # ``held_graph`` answer every other command without reading the catalog.
    if taken := sorted(spec.name for spec in fixed if automation_runs.runs_procedure(spec.name)):
        raise IntegrationGraphError(
            [
                IntegrationViolation(
                    "capability_in_procedure_namespace",
                    f"{', '.join(taken)} is named as an adopted playbook's command is (automation.<id>.run).",
                    tuple(taken),
                )
            ]
        )
    capabilities = (*fixed, *automation_runs.capability_specs(generation))
    # Each field of HQ's own commands says what it is, on its form and to a
    # client; an extension's words are its own repository's to hold.
    if undescribed := sorted(
        f"{spec.name}.{name}"
        for spec in capabilities
        if spec.command_type.__module__.startswith("hq.")
        for name, said in fields(command_schema(spec.command_type)).items()
        if not str(said.get("description", "")).strip()
    ):
        raise IntegrationGraphError(
            [
                IntegrationViolation(
                    "undescribed_command_field",
                    f"Describe {', '.join(undescribed)} in its Annotated type with Describe(...).",
                    tuple(undescribed),
                )
            ]
        )
    return compile_integration_graph(
        capabilities=capabilities,
        resources=(*host_specs("resources"), *plugins.plugin_resource_specs()),
        connections=(*host_specs("connections"), *plugins.plugin_connection_specs()),
        search=plugin_search_definitions(),
    )


GRAPH_OVERRIDE: ContextVar[IntegrationGraph | None] = ContextVar("integration_graph_override", default=None)


def graph_at(generation: str) -> IntegrationGraph:
    return GRAPH_OVERRIDE.get() or compiled(plugins.composition(), generation)


def integration_graph() -> IntegrationGraph:
    """The graph at the catalog adopted now, which a request reads once."""

    return graph_at(automation_adoption.generation())
