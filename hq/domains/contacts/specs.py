"""Contact submissions' commands and resource, declared on their domain in application/domains.py."""

from typing import NotRequired, TypedDict

from pydantic import Field

from hq.domains.control_plane.provider_adapters.cloudflare import ContactStatus
from hq.platform.application import contact_submissions
from hq.platform.application.contact_submissions import (
    ContactDeleteCommand,
    ContactListCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_list,
    execute_contact_review,
)
from hq.platform.application.integration_specs import CapabilitySpec, ResourceSpec
from hq.platform.application.resources import STATUS_FILTER, TEXT_FILTER, BoundedQuery
from hq.platform.application.security import Capability


class ContactSubmissionQuery(BoundedQuery):
    status: ContactStatus | None = Field(default=None, description=STATUS_FILTER)
    query: str = Field(default="", description=TEXT_FILTER)


class ContactSubmissionRow(TypedDict):
    """One message as HQ last read it from the form's table, with a review it asked for laid over.

    A field the table left empty may be absent; the message's own fields are
    the contact form's, its metadata the visitor's browser's.
    """

    id: int
    connection_ref: NotRequired[str]
    account_id: NotRequired[str]
    database_id: NotRequired[str]
    database: NotRequired[str]
    created_at: NotRequired[str | None]
    updated_at: NotRequired[str | None]
    name: NotRequired[str]
    email: NotRequired[str]
    message: NotRequired[str]
    status: NotRequired[str]
    assigned_to: NotRequired[str]
    admin_notes: NotRequired[str]
    country: NotRequired[str]
    ip_address: NotRequired[str]
    browser: NotRequired[str]
    device: NotRequired[str]
    user_agent: NotRequired[str]
    source_url: NotRequired[str]
    turnstile: NotRequired[str]
    # Whether a review HQ asked for is not in the table yet.
    pending: bool
    message_preview: str


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "contact.submissions.list",
            "List the messages sent through the contact form.",
            "read",
            Capability.MANAGE_CONTACTS,
            ContactListCommand,
            execute_contact_list,
            subject_resource="contact.submissions",
            execution_notes=(
                "Validate the requested status and result bound locally.",
                "Read submissions from the stored Cloudflare API reading.",
                "Return only the requested bounded result set.",
            ),
            label="List messages",
        ),
        CapabilitySpec(
            contact_submissions.REVIEW,
            # Its last sentence is records.KEPT, which test_record_updates_told holds it to.
            "Mark one message read, replied, archived or spam, and keep a note on it. "
            "A field left out keeps its value.",
            "remote_write",
            Capability.MANAGE_CONTACTS,
            ContactReviewCommand,
            execute_contact_review,
            "integer",
            "contact.submissions",
            target_label="Message",
            target_help="The message to review.",
            execution_notes=(
                "Read the selected submission and validate its new review state.",
                "Queue the review fields for the controller, which writes them through the Cloudflare API connection.",
                "Record the attributed change in HQ's audit log.",
            ),
            label="Review a message",
        ),
        CapabilitySpec(
            contact_submissions.DELETE,
            "Delete one message sent through the contact form.",
            "destructive",
            Capability.MANAGE_CONTACTS,
            ContactDeleteCommand,
            execute_contact_delete,
            "integer",
            "contact.submissions",
            target_label="Message",
            target_help="The message to delete.",
            execution_notes=(
                "Require confirmation that exactly matches the selected submission ID.",
                "Queue the deletion for the controller, which applies it through the Cloudflare API connection.",
                "Treat an already-absent or already-queued record as a successful retry and audit the change.",
            ),
            label="Delete a message",
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "contact.submissions",
            "Contact submissions",
            "The messages sent through the site's contact form, as HQ last read them, each with its review.",
            Capability.MANAGE_CONTACTS,
            contact_submissions.list_contact_submissions,
            ContactSubmissionQuery,
            contact_submissions.get_contact_submission,
            "id",
            int,
            web_route="contacts:list",
            row_type=ContactSubmissionRow,
            record_type=ContactSubmissionRow,
        ),
    )
