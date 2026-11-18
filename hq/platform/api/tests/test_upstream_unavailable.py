"""A call whose service outside HQ does not answer is refused as unavailable,
503 at the API, on every door: the request was sound and asking again may
succeed, which a 500 would not say."""

import httpx2
from django.test import TestCase

from hq.platform.application.tests.network import PUBLIC, answering, resolving

from .doors import OPERATOR, Caller, at_the_door, each_door

LOOKER = Caller("example-looker", "read look_up_public_records")


def _refused(request):
    raise httpx2.ConnectError("Connection refused", request=request)


class UpstreamUnavailableTests(TestCase):
    def setUp(self):
        at_the_door(self)
        self.enterContext(resolving({"dns-lookup.com": PUBLIC}))
        self.enterContext(answering(_refused))

    def test_a_lookup_whose_resolver_does_not_answer_is_refused_as_unavailable(self):
        for caller in (LOOKER, OPERATOR):
            for door in each_door(self, caller):
                with door:
                    answer = door.capability("lookup.name.read", {"name": "example.com"})

                    self.assertEqual(answer["error"]["code"], "upstream_unavailable", answer)
