"""Contact submissions' commands and resource, declared on their domain in application/domains.py."""

from typing import Annotated, NotRequired, TypedDict

from pydantic import Field

from hq.domains.control_plane.provider_adapters.cloudflare import ContactStatus
from hq.platform.application import contact_submissions
from hq.platform.application.contact_submissions import (
    ContactDeleteCommand,
    ContactReviewCommand,
    execute_contact_delete,
    execute_contact_review,
)
from hq.platform.application.describe import Describe
from hq.platform.application.integration_specs import CapabilitySpec, ResourceSpec
from hq.platform.application.resources import STATUS_FILTER, TEXT_FILTER, BoundedQuery
from hq.platform.application.security import Capability

from .inbox import PREVIEW_LENGTH


class ContactSubmissionQuery(BoundedQuery):
    status: ContactStatus | None = Field(default=None, description=STATUS_FILTER)
    query: str = Field(default="", description=TEXT_FILTER)


class ContactSubmissionRow(TypedDict):
    """One message as HQ last read it from the form's table, with a review it asked for laid over.

    A field the table left empty may be absent; the message's own fields are
    the contact form's, its metadata the visitor's browser's.
    """

    id: Annotated[int, Describe("The message's id in the form's table, which the review and delete commands take.")]
    connection_ref: NotRequired[Annotated[str, Describe("The Cloudflare connection it was read through, by name.")]]
    account_id: NotRequired[Annotated[str, Describe("The id of the Cloudflare account that holds the table.")]]
    database_id: NotRequired[Annotated[str, Describe("The id of the D1 database that holds the table.")]]
    database: NotRequired[Annotated[str, Describe("The name of the D1 database that holds the table.")]]
    created_at: NotRequired[
        Annotated[str | None, Describe("When it was sent, an ISO 8601 date-time; null when the table says nothing.")]
    ]
    updated_at: NotRequired[
        Annotated[
            str | None,
            Describe("When the table last changed it, an ISO 8601 date-time; null when the table says nothing."),
        ]
    ]
    name: NotRequired[Annotated[str, Describe("The name the visitor gave.")]]
    email: NotRequired[Annotated[str, Describe("The email address the visitor gave.")]]
    message: NotRequired[Annotated[str, Describe("What the visitor wrote, in full.")]]
    status: NotRequired[contact_submissions.ReviewStatus]
    assigned_to: NotRequired[contact_submissions.Assignee]
    admin_notes: NotRequired[contact_submissions.AdminNotes]
    country: NotRequired[Annotated[str, Describe("The country it was sent from, as the site recorded it.")]]
    ip_address: NotRequired[Annotated[str, Describe("The IP address it was sent from.")]]
    browser: NotRequired[Annotated[str, Describe("The visitor's browser, as the site recorded it.")]]
    device: NotRequired[Annotated[str, Describe("The type of device it was sent from, as the site recorded it.")]]
    user_agent: NotRequired[Annotated[str, Describe("The visitor's browser's whole user agent string.")]]
    source_url: NotRequired[Annotated[str, Describe("The page the form was sent from.")]]
    turnstile: NotRequired[
        Annotated[str, Describe("What the form's bot check found, as the site recorded it, such as `verified`.")]
    ]
    pending: Annotated[
        bool,
        Describe("Whether a review HQ asked for is not in the table yet; the fields it changes already show it."),
    ]
    message_preview: Annotated[str, Describe(f"The message's first {PREVIEW_LENGTH} characters, for a list to show.")]


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
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
