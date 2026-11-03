from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

BODY = b"<!doctype html><p>" + b"an answer worth compressing " * 100


def _answering(*, etag=None, body=BODY, content_type=b"text/html; charset=utf-8", encoding=None):
    async def app(scope, receive, send):
        headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
        if etag:
            headers.append((b"etag", etag.encode()))
        if encoding:
            headers.append((b"content-encoding", encoding))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


def _get(app, accepted):
    import httpx2

    from hq.platform.core.compression import Compressed

    async def fetch():
        transport = httpx2.ASGITransport(app=Compressed(app))
        async with (
            httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
            client.stream("GET", "/", headers={"Accept-Encoding": accepted}) as response,
        ):
            return response, b"".join([chunk async for chunk in response.aiter_raw()])

    return async_to_sync(fetch)()


class CompressedTests(SimpleTestCase):
    """An answer is sent in the smallest coding the client takes, and an encoded
    one never claims the strong validator of the bytes it replaced."""

    def test_the_smallest_coding_the_client_takes_is_the_one_sent(self):
        # Brotli over Zstandard and gzip, whatever the weights, since it is the smallest.
        for accepted, coding in (
            ("gzip, deflate, br, zstd", "br"),
            ("zstd;q=1, br;q=0.1", "br"),
            ("gzip", "gzip"),
            ("zstd", None),
            ("identity", None),
            ("br;q=0, gzip;q=0", None),
        ):
            with self.subTest(accepted=accepted):
                response, _ = _get(_answering(), accepted)
                self.assertEqual(response.headers.get("content-encoding"), coding)
                self.assertIn("Accept-Encoding", response.headers["vary"])

    def test_an_encoded_answer_carries_its_validator_weakened(self):
        for accepted in ("br", "gzip"):
            with self.subTest(accepted=accepted):
                response, _ = _get(_answering(etag='"abc"'), accepted)
                self.assertEqual(response.headers["content-encoding"], accepted)
                self.assertEqual(response.headers["etag"], 'W/"abc"')

    def test_an_answer_sent_as_it_is_keeps_its_validator(self):
        for etag in ('"abc"', 'W/"abc"'):
            with self.subTest(etag=etag):
                response, _ = _get(_answering(etag=etag), "identity")
                self.assertNotIn("content-encoding", response.headers)
                self.assertEqual(response.headers["etag"], etag)
        response, _ = _get(_answering(etag='W/"abc"'), "br")
        self.assertEqual(response.headers["etag"], 'W/"abc"')

    def test_an_answer_already_encoded_is_sent_with_its_own_validator(self):
        response, sent = _get(_answering(encoding=b"br", etag='"abc"'), "br, gzip")
        self.assertEqual(sent, BODY)
        self.assertEqual(response.headers["content-encoding"], "br")
        self.assertEqual(response.headers["etag"], '"abc"')

    def test_an_answer_that_is_not_text_is_sent_as_it_is(self):
        response, sent = _get(_answering(content_type=b"application/pdf", etag='"abc"'), "br, gzip")
        self.assertEqual(sent, BODY)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.headers["etag"], '"abc"')

    def test_a_short_answer_is_sent_as_it_is(self):
        response, sent = _get(_answering(body=b"<p>short</p>"), "br")
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(sent, b"<p>short</p>")
