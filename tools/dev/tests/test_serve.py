"""A development server on loopback answers on both stacks, and on nothing past loopback."""

import socket
from unittest import mock, skipUnless

from django.test import SimpleTestCase


def answers(address: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    with socket.socket(family) as client:
        client.settimeout(5)
        return client.connect_ex((address, port)) == 0


def outside_address() -> str:
    """An address of this machine's own past loopback: the one its route out leaves from."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        # A datagram socket's connect sends nothing; it only picks the route.
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]


@skipUnless(socket.has_ipv6, "this machine has no IPv6 stack")
class ListenersTests(SimpleTestCase):
    def listen(self, host: str) -> list[socket.socket]:
        from tools.dev.serve import listeners

        bound = listeners(host, 0)
        for sock in bound:
            self.addCleanup(sock.close)
            sock.listen()
        return bound

    def test_a_loopback_host_is_answered_on_both_stacks_at_one_port(self):
        bound = self.listen("127.0.0.1")
        port = bound[0].getsockname()[1]

        self.assertEqual({sock.getsockname()[1] for sock in bound}, {port})
        self.assertTrue(answers("127.0.0.1", port))
        self.assertTrue(answers("::1", port))

    def test_nothing_past_loopback_is_answered(self):
        outside = outside_address()
        port = self.listen("localhost")[0].getsockname()[1]

        self.assertFalse(answers(outside, port))


class ServeTests(SimpleTestCase):
    def test_it_serves_the_development_application_without_reading_proxy_headers(self):
        """The application reads its proxy's forwarded headers itself, after recording the peer they replace."""

        from tools.dev import serve

        with (
            mock.patch.object(serve, "Config") as configured,
            mock.patch.object(serve, "Server"),
            mock.patch.object(serve, "ChangeReload"),
            mock.patch.object(serve, "listeners", side_effect=self.bound),
            mock.patch.dict("os.environ"),
            mock.patch("sys.argv", ["serve", "--host", "127.0.0.1", "--port", "0"]),
        ):
            serve.main()

        self.assertEqual(configured.call_args.args, (serve.APPLICATION,))
        self.assertIs(configured.call_args.kwargs["proxy_headers"], False)

    def bound(self, host: str, port: int) -> list[socket.socket]:
        sock = socket.create_server((host, port))
        self.addCleanup(sock.close)
        return [sock]
