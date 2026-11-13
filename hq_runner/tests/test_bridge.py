"""The worker reaches HQ only over a bridge socket it trusts, sends each action
as the bridge contract names it, and turns any answer the contract does not
allow into an error, never a value."""

import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, override
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from hq_runner.bridge import Bridge, BridgeError


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class BridgeTestCase(unittest.TestCase):
    """A stand-in for HQ's bridge application on a real Unix socket."""

    answer: tuple[int, str, bytes] = (200, "application/json", b'{"ok": true, "operation": null}')

    def setUp(self):
        # A short path: a Unix socket's path is bounded (104 bytes on macOS).
        directory = Path(os.path.realpath(tempfile.mkdtemp(prefix="hqr-", dir="/tmp")))
        self.addCleanup(lambda: (self.socket.unlink(missing_ok=True), directory.rmdir()))
        directory.chmod(0o700)
        self.socket = directory / "bridge.sock"
        self.requests: list[dict[str, Any]] = []
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                parts = urlsplit(self.path)
                test.requests.append(
                    {"path": parts.path, "query": parse_qs(parts.query), "body": self.rfile.read(length)}
                )
                status, media, body = test.answer
                self.send_response(status)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            @override
            def log_message(self, format, *args):
                del format, args

        server = _Server(str(self.socket), Handler)
        self.socket.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.bridge = Bridge(str(self.socket))


class CallTests(BridgeTestCase):
    def test_a_claim_names_its_controller_capability_lease_and_attempt(self):
        self.answer = (200, "application/json", b'{"ok": true, "operation": null}')

        answer = self.bridge.claim(
            "example-worker", capability="automation.operation:run", lease_seconds=120, attempt="a1"
        )

        self.assertEqual(answer, {"ok": True, "operation": None})
        (sent,) = self.requests
        self.assertEqual(sent["path"], "/claim")
        self.assertEqual(
            sent["query"],
            {
                "controller-id": ["example-worker"],
                "lease-seconds": ["120"],
                "capability": ["automation.operation:run"],
                "attempt": ["a1"],
            },
        )

    def test_a_report_is_its_json_body(self):
        self.answer = (200, "application/json", b'{"ok": true}')
        report = {"success": True, "observed_generation": 2, "message": "", "events": []}

        self.bridge.report("example-worker", "op-1", report)

        (sent,) = self.requests
        self.assertEqual((sent["path"], sent["query"]["operation"]), ("/report", ["op-1"]))
        self.assertEqual(json.loads(sent["body"]), report)

    def test_a_refusal_is_an_error_naming_hq_s_reason(self):
        self.answer = (
            400,
            "application/problem+json",
            b'{"title": "Bad", "status": 400, "detail": "Operation lease has expired."}',
        )

        with self.assertRaisesRegex(BridgeError, "Operation lease has expired"):
            self.bridge.renew("example-worker", "op-1", lease_seconds=120)

    def test_an_answer_that_is_not_a_json_object_is_an_error(self):
        for answer in (
            (200, "text/html", b"<p>hi</p>"),
            (200, "application/json", b"[1]"),
            (200, "application/json", b"{"),
        ):
            with self.subTest(answer=answer):
                self.answer = answer
                with self.assertRaises(BridgeError):
                    self.bridge.renew("example-worker", "op-1", lease_seconds=120)


class TrustTests(BridgeTestCase):
    def test_a_socket_another_account_may_open_is_refused(self):
        self.socket.chmod(0o666)

        with self.assertRaisesRegex(BridgeError, "mode is not 0600"):
            self.bridge.renew("example-worker", "op-1", lease_seconds=120)
        self.assertEqual(self.requests, [])

    def test_a_directory_open_to_another_account_is_refused(self):
        self.socket.parent.chmod(0o755)

        with self.assertRaisesRegex(BridgeError, "open to another account"):
            self.bridge.renew("example-worker", "op-1", lease_seconds=120)
        self.assertEqual(self.requests, [])

    def test_a_socket_answered_by_another_account_is_refused(self):
        # The kernel's answer for a peer of another account, as a socket
        # swapped in after the path was checked would give it.
        with (
            patch("hq_runner.bridge.peer_uid", return_value=os.geteuid() + 1),
            self.assertRaisesRegex(BridgeError, "answered by another account"),
        ):
            self.bridge.renew("example-worker", "op-1", lease_seconds=120)
        self.assertEqual(self.requests, [])

    def test_the_peer_this_account_is_read_from_the_connection(self):
        self.bridge.renew("example-worker", "op-1", lease_seconds=120)

        self.assertEqual(len(self.requests), 1)

    def test_a_relative_or_unclean_path_is_refused(self):
        for path in ("bridge.sock", f"{self.socket.parent}/../{self.socket.parent.name}/bridge.sock"):
            with self.subTest(path=path), self.assertRaisesRegex(BridgeError, "absolute, clean"):
                Bridge(path).renew("example-worker", "op-1", lease_seconds=120)
