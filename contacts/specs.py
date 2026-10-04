"""Contact submissions' commands and resource, declared on their domain in application/domains.py."""

from __future__ import annotations

from application import contact_submissions
from application.contact_submissions import (
    ContactDeleteCommand,
    ContactListCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_list,
    execute_contact_review,
)
from application.integration_specs import CapabilitySpec, ResourceSpec
from application.resources import BoundedQuery
from application.security import Capability


class ContactSubmissionQuery(BoundedQuery):
    status: str = ""
    query: str = ""


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            "contact.submissions.list",
            "List contact submissions held in Cloudflare D1.",
            "read",
            Capability.MANAGE_CONTACTS,
            ContactListCommand,
            execute_contact_list,
            subject_resource="contact.submissions",
            execution_notes=(
                "Validate the requested status and result bound locally.",
                "Read submissions through the configured D1 connection.",
                "Return only the requested bounded result set.",
            ),
            label="List contact submissions",
        ),
        CapabilitySpec(
            "contact.submission.review",
            "Review and update one contact submission in Cloudflare D1.",
            "remote_write",
            Capability.MANAGE_CONTACTS,
            ContactReviewCommand,
            execute_contact_review,
            "integer",
            "contact.submissions",
            target_label="Submission ID",
            target_help="The contact submission to review.",
            execution_notes=(
                "Read the selected submission and validate its new review state.",
                "Write the review fields through the configured D1 connection.",
                "Record the attributed change in HQ's audit log.",
            ),
            label="Review contact submission",
        ),
        CapabilitySpec(
            "contact.submission.delete",
            "Delete one explicitly confirmed contact submission from Cloudflare D1.",
            "destructive",
            Capability.MANAGE_CONTACTS,
            ContactDeleteCommand,
            execute_contact_delete,
            "integer",
            "contact.submissions",
            target_label="Submission ID",
            target_help="The contact submission to delete.",
            execution_notes=(
                "Require confirmation that exactly matches the selected submission ID.",
                "Delete the record through the configured D1 connection.",
                "Treat an already-absent record as a successful retry and audit the change.",
            ),
            label="Delete contact submission",
        ),
    )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "contact.submissions",
            "Contact submissions",
            "Contact requests held in Cloudflare D1 and reviewed through HQ.",
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
