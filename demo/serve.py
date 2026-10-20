"""The built demo, served over plain HTTP the way a static host would serve it."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@contextmanager
def serving(site: Path) -> Iterator[str]:
    """Serve ``site`` on a free local port and yield its origin."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(site)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
