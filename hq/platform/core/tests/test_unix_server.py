"""The private Unix listener: who may bind it, who may call it, and what it leaves behind."""

import http.client
import os
import socket
import stat
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from hq.platform.core import unix_server
from hq.platform.core.unix_server import SocketRefused, peer_uid, private_listener

from .unix_serving import post, served, socket_directory


async def hello(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": str(scope["server"][1]).encode()})


class PrivateListenerTests(SimpleTestCase):
    def setUp(self):
        manager = socket_directory()
        self.directory = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.path = self.directory / "bridge.sock"

    def bound(self, path: Path | None = None) -> socket.socket:
        listener = private_listener(str(path or self.path))
        self.addCleanup(listener.close)
        return listener

    def test_the_socket_is_this_accounts_alone(self):
        self.bound()
        held = os.lstat(self.path)
        self.assertTrue(stat.S_ISSOCK(held.st_mode))
        self.assertEqual(stat.S_IMODE(held.st_mode), 0o600)
        self.assertEqual(held.st_uid, os.geteuid())

    def test_a_directory_another_account_can_enter_is_refused(self):
        for mode in (0o755, 0o750, 0o701, 0o720):
            with self.subTest(mode=oct(mode)):
                self.directory.chmod(mode)
                with self.assertRaisesMessage(SocketRefused, "open to another account"):
                    self.bound()
                self.assertFalse(self.path.exists())

    def test_a_directory_that_is_another_accounts_is_refused(self):
        with (
            patch.object(unix_server.os, "geteuid", return_value=os.geteuid() + 1),
            self.assertRaisesMessage(SocketRefused, "belongs to another account"),
        ):
            self.bound()

    def test_a_linked_directory_is_refused(self):
        link = self.directory / "link"
        with socket_directory() as real:
            link.symlink_to(real)
            with self.assertRaisesMessage(SocketRefused, "not a directory"):
                self.bound(link / "bridge.sock")

    def test_a_missing_directory_is_refused(self):
        with self.assertRaisesMessage(SocketRefused, "does not exist"):
            self.bound(self.directory / "absent" / "bridge.sock")

    def test_a_path_that_is_not_absolute_and_normal_is_refused(self):
        for path in ("bridge.sock", f"{self.directory}/./bridge.sock", f"{self.directory}/../x/bridge.sock"):
            with self.subTest(path=path), self.assertRaisesMessage(SocketRefused, "absolute, normal path"):
                private_listener(path)

    def test_something_else_at_the_path_is_refused_and_left_alone(self):
        self.path.write_text("kept")
        with self.assertRaisesMessage(SocketRefused, "not this account's socket"):
            self.bound()
        self.assertEqual(self.path.read_text(), "kept")

    def test_a_link_at_the_path_is_refused_and_not_followed(self):
        target = self.directory / "elsewhere.sock"
        self.bound(target)
        self.path.symlink_to(target)
        with self.assertRaisesMessage(SocketRefused, "not this account's socket"):
            self.bound()
        self.assertTrue(self.path.is_symlink())
        self.assertTrue(target.exists())

    def test_a_socket_a_previous_run_left_is_replaced(self):
        first = private_listener(str(self.path))
        first.close()
        self.assertTrue(self.path.exists())
        self.bound()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as caller:
            caller.connect(str(self.path))

    def test_the_kernel_names_the_peer(self):
        listener = self.bound()
        listener.setblocking(True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as caller:
            caller.connect(str(self.path))
            accepted, _ = listener.accept()
            self.addCleanup(accepted.close)
            self.assertEqual(peer_uid(accepted), os.geteuid())
            self.assertEqual(peer_uid(caller), os.geteuid())

    def test_a_peer_that_cannot_be_named_is_nobody(self):
        class Unreadable:
            def getsockopt(self, *args):
                raise OSError("not a socket")

        self.assertIsNone(peer_uid(Unreadable()))
        with patch.object(unix_server.sys, "platform", "example-os"):
            self.assertIsNone(peer_uid(Unreadable()))


class ServingTests(SimpleTestCase):
    def setUp(self):
        manager = socket_directory()
        self.directory = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        self.path = self.directory / "bridge.sock"

    def test_this_account_is_served_on_a_unix_listener_and_the_socket_is_removed_after(self):
        with served(hello, self.path):
            # ASGI names a Unix listener with no port.
            self.assertEqual(post(self.path, "/"), (200, b"None"))
            self.assertEqual(stat.S_IMODE(os.lstat(self.path).st_mode), 0o600)
        self.assertFalse(self.path.exists())

    def test_another_accounts_connection_is_dropped_unheard(self):
        for answered in (os.geteuid() + 1, 0, None):
            with self.subTest(peer=answered), patch.object(unix_server, "peer_uid", return_value=answered):
                with (
                    served(hello, self.path),
                    self.assertLogs("severino.unix_server", "WARNING") as logs,
                    self.assertRaises((OSError, http.client.HTTPException)),
                ):
                    post(self.path, "/")
                self.assertIn("peer_refused", logs.output[0])

    def test_a_path_that_cannot_be_served_safely_stops_the_server_from_starting(self):
        self.directory.chmod(0o755)
        with self.assertRaises(SocketRefused), served(hello, self.path):
            self.fail("served on a directory another account can enter")
