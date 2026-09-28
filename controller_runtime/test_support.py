"""Helpers the controller's test modules share. Holds no tests."""

from __future__ import annotations

import json
from pathlib import Path

from . import providers


def _controller_source() -> str:
    """Every module the controller process runs, as one text.

    Properties of the process (what it may hold, what it never caches) are
    asserted over all of it, so moving code between its modules cannot move
    it out of reach of the check.
    """

    folder = Path(providers.__file__).resolve().parent
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(folder.glob("*.py"))
        if not path.name.startswith("test")
    )


def _by_url(routes):
    """Answer a mocked provider request by what it asked for, not by call order.

    Order-indexed fakes encode the sweep's iteration order into every test that
    uses one, so adding a connection rewrites tests that have nothing to do
    with it.
    """

    def respond(url, *args, **kwargs):
        # Longest match wins. `/tokens` is a substring of `/user/tokens/verify`,
        # so first-match would answer Cloudflare with NPM's reply.
        matches = sorted((fragment for fragment in routes if fragment in url), key=len)
        if not matches:
            raise AssertionError(f"Unexpected provider request: {url}")
        return routes[matches[-1]]

    return respond


def _bridge(**responses):
    """Answer a mocked bridge call by which action it is, not by call order.

    Order-indexed fakes encode the pass's exact sequence into every test that
    uses one, so adding a step rewrites tests that have nothing to do with it.
    """

    def respond(*args, **kwargs):
        action = args[0] if args else ""
        if action in responses:
            return responses[action]
        if action == "glance-plan":
            return {"ok": True, "panels": [], "targets": {}}
        raise AssertionError(f"Unexpected bridge call: {action}")

    return respond


class _Answer:
    """The context-manager shape `urlopen` returns."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Page:
    """A stubbed urlopen answer: where it landed, its content type and body."""

    def __init__(self, body, *, landed, content_type="application/json"):
        from email.message import Message

        self._body = body
        self._landed = landed
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def geturl(self):
        return self._landed

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    """A loopback HTTP server that records each request and answers as told."""

    def __init__(self, answer):
        import http.server
        import threading

        self.seen = []
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                recorder.seen.append(
                    (self.command, self.path, dict(self.headers), self.rfile.read(length))
                )
                status, headers, body = answer(self)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False


# One account, three sites; the first carries its aliases, as shared hosting
# serves `www.` and a parked domain from the main site.
_ACCOUNT_SITES = {
    "sites": {
        "example.test": ["example.test", "www.example.test", "parked.example"],
        "shop.example.test": ["shop.example.test", "www.shop.example.test"],
        "lab.example.test": ["lab.example.test"],
    }
}


A_RECORDED_CERTIFICATE = {
    "certificate_name": "an-example-certificate",
    "domains": ["shop.example.test", "*.shop.example.test"],
    "renewal_window_days": 30,
    "consumers": [
        {
            "kind": "npm",
            "name": "an-example-certificate-npm",
            "connection_ref": "a-proxy",
            "verify_domains": ["shop.example.test"],
        }
    ],
    "publish_to": [
        {
            "kind": "onepassword",
            "name": "an-example-certificate-onepassword",
            "connection_ref": "a-password-manager",
            "vault": "An Example Vault",
            "item": "An Example Certificate Item",
        }
    ],
}

AN_OBSERVATION = {
    "issuer": "An Example Authority",
    "not_after": "2027-01-14T09:30:00+00:00",
    "expected_fingerprint_sha256": "aa:bb:cc",
    "consumers": [
        {
            "consumer": "an-example-certificate-npm",
            "domain": "shop.example.test",
            "fingerprint_sha256": "aa:bb:cc",
        }
    ],
}
