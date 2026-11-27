"""The hops a request came through: which one HQ believes, the headers it read, and the ones it refuses."""

from django.test import RequestFactory, TestCase, override_settings

from hq.platform.core.tests.forwarding import arrived

from ..request_channel import forwarding_of
from ..request_path import address_chain


@override_settings(
    ALLOWED_HOSTS=["hq.example.test", "testserver"],
    SEVERINO_TRUSTED_PROXIES=["10.0.0.0/8", "172.16.0.0/12"],
)
class HopTests(TestCase):
    """Which hop HQ believes, and the working shown for it.

    The quietly consequential decision on the page: believe the wrong hop and
    the network gate is judging a proxy rather than a caller, which it does
    silently and looks identical from every other surface.
    """

    def hops(self, *, peer, forwarded=""):
        request = RequestFactory().get(
            "/connection/",
            HTTP_HOST="hq.example.test",
            **arrived(REMOTE_ADDR=peer, **({"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {})),
        )
        return address_chain(forwarding_of(request))

    def test_a_chain_of_only_proxies_says_nothing_identified_the_caller(self):
        """The shape a proxy that drops the client address produces."""

        found = self.hops(peer="10.0.0.9", forwarded="172.18.0.1")

        judged = next(hop for hop in found if hop.role == "judged")
        # HQ falls back to the first proxy's address, as the forwarded-header
        # middleware does.
        self.assertEqual(judged.value, "172.18.0.1")
