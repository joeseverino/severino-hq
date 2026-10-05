"""A request cannot wait on anything outside the process, whatever it calls."""

from __future__ import annotations

import socket
import subprocess
import threading
import time

from django.test import RequestFactory, SimpleTestCase, override_settings

from hq.platform.core import outbound
from hq.platform.core.outbound import OutboundInRequest, allowed, off_request, serving


def reach_a_network():
    # A documentation address: nothing answers there, and the connect is
    # refused by the hook before any packet is addressed to it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.01)
        probe.connect(("192.0.2.1", 9))


ATTEMPTS = {
    "a connection": reach_a_network,
    "a name lookup": lambda: socket.getaddrinfo("example.com", 443),
    "a process": lambda: subprocess.run(["true"], check=False),
    "a sleep": lambda: time.sleep(0),
}


class RefusalTests(SimpleTestCase):
    def setUp(self):
        self.request = RequestFactory().post("/example/refresh/")

    def test_a_request_is_refused_every_way_of_waiting_on_the_outside(self):
        for name, attempt in ATTEMPTS.items():
            with self.subTest(name), serving(self.request), self.assertRaises(OutboundInRequest) as refused:
                attempt()
            self.assertIn("POST /example/refresh/ tried", str(refused.exception))

    def test_outside_a_request_nothing_is_refused(self):
        time.sleep(0)
        subprocess.run(["true"], check=False)

    def test_a_declared_exception_may_wait(self):
        with serving(self.request), allowed("oidc"):
            time.sleep(0)

    def test_an_exception_nobody_declared_is_not_one(self):
        with self.assertRaises(ValueError):
            with allowed("because-i-say-so"):
                pass

    def test_a_job_is_not_the_request_that_started_it(self):
        ran = []

        def work():
            with off_request():
                time.sleep(0)
                ran.append(True)

        with serving(self.request):
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
            with off_request():
                time.sleep(0)

        self.assertEqual(ran, [True])

    def test_a_datagram_connect_sends_nothing_and_is_let_through(self):
        with serving(self.request), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))

    @override_settings(SEVERINO_OUTBOUND_IN_REQUEST="report")
    def test_a_composition_may_report_instead_of_refusing(self):
        with serving(self.request), self.assertLogs("severino.request", "WARNING") as logged:
            time.sleep(0)

        self.assertEqual(logged.records[0].event, "outbound.in_request")
        self.assertEqual(logged.records[0].audit_event, "time.sleep")

    def test_the_default_is_to_refuse(self):
        self.assertEqual(outbound.mode(), outbound.REFUSE)
