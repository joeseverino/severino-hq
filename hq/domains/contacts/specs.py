"""Contact submissions' commands and resource, declared on their domain in application/domains.py."""

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
from hq.platform.application.resources import BoundedQuery
from hq.platform.application.security import Capability


class ContactSubmissionQuery(BoundedQuery):
    status: str = ""
    query: str = ""


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
            "contact.submission.review",
            "Mark one message read, replied, archived or spam, and keep a note on it.",
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
            "contact.submission.delete",
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
            "Contact requests held in Cloudflare D1, as the controller last read them, and reviewed through HQ.",
            Capability.MANAGE_CONTACTS,
            contact_submissions.list_contact_submissions,
            ContactSubmissionQuery,
            contact_submissions.get_contact_submission,
            "id",
            int,
            not_found_errors=(contact_submissions.ContactSubmissionNotFound,),
            web_route="contacts:list",
        ),
    )
