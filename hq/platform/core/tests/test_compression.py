from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

BODY = b"<!doctype html><p>" + b"an answer worth compressing " * 100


def _answering(*, etag):
    async def app(scope, receive, send):
        headers = [
            (b"content-type", b"text/html; charset=utf-8"),
            (b"content-length", str(len(BODY)).encode()),
            (b"etag", etag.encode()),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": BODY})

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
            return response

    return async_to_sync(fetch)()


class CompressedTests(SimpleTestCase):
    """An encoded answer never claims the strong validator of the bytes it replaced."""

    def test_an_encoded_answer_carries_its_validator_weakened(self):
        for accepted in ("br", "gzip"):
            with self.subTest(accepted=accepted):
                response = _get(_answering(etag='"abc"'), accepted)
                self.assertEqual(response.headers["content-encoding"], accepted)
                self.assertEqual(response.headers["etag"], 'W/"abc"')
