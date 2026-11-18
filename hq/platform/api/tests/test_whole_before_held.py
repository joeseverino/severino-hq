"""A capability's payload is whole before anything else is done with it: a
malformed declaration is refused as invalid input at every door, and never
becomes a held request for a person to approve."""

from django.test import TestCase

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource
from hq.platform.application.tests.host_only import host_only

from .doors import OPERATOR, Caller, at_the_door, each_door

AGENT = Caller("example-agent", "read manage_infrastructure")
READER = Caller("example-agent", "read")
CREATE = "infrastructure.resource.create"
ZONE = {"zone": "example.com", "connection_ref": "example-dns"}
# A kind no provider declares, a kind only its own domain writes, and
# declarations of a kind that waits for a person (a zone's, which changes what
# the public internet sees) whose spec its kind does not take, or whose key
# the record's column does not.
MALFORMED = (
    {"kind": "__not_in_enum__", "key": "a", "spec": {}},
    {"kind": "cloudflare.contact_inbox", "key": "a", "spec": {}},
    {"kind": "cloudflare.zone", "key": "a", "spec": {"junk": 1}},
    {"kind": "cloudflare.zone", "key": "", "spec": {}},
    {"kind": "cloudflare.zone", "key": "", "spec": ZONE},
    {"kind": "cloudflare.zone", "key": "not a key", "spec": ZONE},
    {"kind": "cloudflare.zone", "key": "k" * 181, "spec": ZONE},
)


class WholeBeforeHeldTests(TestCase):
    def setUp(self):
        at_the_door(self)
        self.enterContext(host_only())

    def test_a_malformed_declaration_is_refused_as_invalid_input_and_never_held(self):
        for asker in (AGENT, OPERATOR):
            for door in each_door(self, asker):
                for number, payload in enumerate(MALFORMED):
                    with door, self.subTest(payload=payload):
                        answer = door.capability(CREATE, {"idempotency_key": f"example-{number}", **payload})

                        self.assertFalse(answer["ok"], answer)
                        self.assertEqual(answer["error"]["code"], "invalid_input", answer)
                        self.assertFalse(ApprovalRequest.objects.exists())
                        self.assertFalse(ManagedResource.objects.exists())

    def test_a_caller_without_the_grant_is_refused_before_the_payload_is_read(self):
        for door in each_door(self, READER):
            with door:
                answer = door.capability(CREATE, {"idempotency_key": "example-0", **MALFORMED[2]})

                self.assertEqual(answer["error"]["code"], "insufficient_scope", answer)
                self.assertFalse(ApprovalRequest.objects.exists())
