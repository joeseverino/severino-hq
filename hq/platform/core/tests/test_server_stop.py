"""Each of HQ's servers stops within its graceful bound, a stream still open included.

Uvicorn otherwise waits for every open connection to close, and a listen
stream stays open until its token expires, so a stop would wait out the
host's kill instead.
"""

import asyncio
import threading
from unittest import mock

import uvicorn
from django.test import SimpleTestCase

from hq.config import serve
from hq.platform.core import unix_server

from .unix_serving import UnixConnection, served, socket_directory


class ServerStopTests(SimpleTestCase):
    def test_the_image_server_is_given_the_bound(self):
        with mock.patch.object(uvicorn.Config, "configure_logging"):
            bound = serve.config().timeout_graceful_shutdown

        self.assertIsNotNone(bound, "the image's server waits on open streams")
        self.assertEqual(bound, serve.GRACEFUL_SHUTDOWN_SECONDS)

    def test_a_stream_open_on_the_bridge_is_cut_and_its_stop_ends(self):
        """The web's lifespan ends with the bridge's server, so a stop waits on its streams too."""

        cut = threading.Event()

        async def streaming(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"open", "more_body": True})
            try:
                # Open until its client leaves, as a listen stream is.
                while (await receive())["type"] != "http.disconnect":
                    pass
            except asyncio.CancelledError:
                cut.set()
                raise

        directory = socket_directory()
        path = directory.__enter__() / "bridge.sock"
        self.addCleanup(directory.__exit__, None, None, None)
        stream = UnixConnection(path, timeout=5)
        self.addCleanup(stream.close)
        # A bound of none, so the test waits out no span of its own; created,
        # so a server that reads no bound is caught waiting on the stream.
        with mock.patch.object(unix_server, "GRACEFUL_SHUTDOWN_SECONDS", 0, create=True), served(streaming, path):
            stream.request("GET", "/")
            self.assertEqual(stream.getresponse().status, 200)

        self.assertTrue(cut.is_set(), "the stream still open at the stop was never cut")
        self.assertFalse(path.exists(), "the bridge's server never finished stopping")
