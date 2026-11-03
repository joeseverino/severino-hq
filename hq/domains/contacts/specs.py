"""Contact submissions' commands and resource, declared on their domain in application/domains.py."""

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
        ),
    )
