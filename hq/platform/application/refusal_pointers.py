"""What gets past a refusal, as its details point to it.

It names the operation a run holds, whose module loads models, so
`refusal_status`, which loads before them, reads it only through the
`RefusalDetails` alias, whose value is read when a schema is built.
"""

from typing import Annotated, Any, TypedDict

from pydantic import Field

from .refusal_status import InputProblem, NextCall
from .resource_operations import OperationItem


class MissingConnection(TypedDict):
    machine: Annotated[str, Field(description="The machine the runner cannot reach.")]
    profile: Annotated[str, Field(description="The login the playbook runs as.")]


class PlaybookAdmits(TypedDict):
    """What a playbook is written for."""

    target_kinds: Annotated[list[str], Field(description="The kinds of record it runs against.")]
    hosts: Annotated[list[str], Field(description="The machines it names.")]


class RefusalPointers(TypedDict, total=False, extra_items=Any):
    """What gets past a refusal, each part where the refusal has it: `next`
    is the call that finds what was missing, `holding` the run in the way,
    `operation` the one the refused change asked for, and `admits` what the
    playbook refused is written for."""

    next: NextCall
    closest: Annotated[
        list[NextCall], Field(description="The read of each record or list named nearest the one asked for.")
    ]
    field: Annotated[str, Field(description="The argument to send.")]
    scope: Annotated[str, Field(description="The permission to grant the token's client.")]
    identifier: Annotated[str, Field(description="The field the resource's records are addressed by.")]
    errors: Annotated[list[InputProblem], Field(description="Each field of a list or record read that was wrong.")]
    filters: Annotated[list[str], Field(description="What the resource's list narrows by.")]
    unknown: Annotated[list[str], Field(description="Each host named that is no machine HQ knows.")]
    refused: Annotated[list[str], Field(description="Each machine, or each reason, refused.")]
    admits: PlaybookAdmits
    missing: Annotated[list[MissingConnection], Field(description="Each machine the runner has no connection to.")]
    holding: OperationItem
    playbook: Annotated[str, Field(description="The playbook of the run in the way.")]
    machines: Annotated[list[str], Field(description="The machines the run in the way is on.")]
    shared: Annotated[list[str], Field(description="The machines asked for that the run in the way holds.")]
    operation: OperationItem
