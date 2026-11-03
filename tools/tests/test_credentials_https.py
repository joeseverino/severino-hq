"""The minting's one HTTP request: its token in a header, a redirect an answer, a failure free of secrets."""

from __future__ import annotations

import json
from unittest import mock

import httpx2
from django.test import SimpleTestCase

from tools.credentials import Call, Refused, https

TOKEN = "bootstrap-secret-0123456789"
SECRET = "client-secret-9876543210"
API = "https://api.example.test"


class Transport:
    """What the network answers, and every request that reached it."""

    def __init__(self, answer: httpx2.Response | Exception) -> None:
        self.answer = answer
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def sent(answer: httpx2.Response | Exception, call: Call) -> tuple[Transport, object]:
    transport = Transport(answer)
    with mock.patch.object(httpx2.HTTPTransport, "handle_request", side_effect=transport):
        return transport, https(call)


class HttpsTests(SimpleTestCase):
    def test_the_token_travels_in_the_header_and_never_the_url(self):
        transport, answer = sent(httpx2.Response(200, json={"ok": True}), Call("GET", f"{API}/tokens", token=TOKEN))

        (request,) = transport.requests
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, str(request.url))

    def test_a_redirect_is_the_answer_and_no_second_request_follows(self):
        moved = httpx2.Response(302, headers={"Location": "https://elsewhere.example.test/"}, json={"moved": True})

        transport, answer = sent(moved, Call("GET", f"{API}/tokens", token=TOKEN))

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(answer, {"moved": True})

    def test_a_refusal_answers_its_json_and_a_body_that_is_not_json_answers_none(self):
        _, refused = sent(httpx2.Response(403, json={"errors": ["no"]}), Call("GET", f"{API}/tokens"))
        _, unreadable = sent(httpx2.Response(502, text="<html>"), Call("GET", f"{API}/tokens"))

        self.assertEqual((refused, unreadable), ({"errors": ["no"]}, None))

    def test_json_and_a_form_go_as_their_own_content_types(self):
        posted, _ = sent(httpx2.Response(200, json={}), Call("POST", f"{API}/keys", token=TOKEN, json={"a": 1}))
        exchanged, _ = sent(httpx2.Response(200, json={}), Call("POST", f"{API}/oauth", form={"secret": SECRET}))

        self.assertEqual(posted.requests[0].headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(posted.requests[0].content), {"a": 1})
        self.assertEqual(exchanged.requests[0].headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(exchanged.requests[0].content, f"secret={SECRET}".encode())

    def test_an_unreachable_api_is_refused_without_a_secret_in_the_message(self):
        call = Call("POST", f"{API}/oauth", token=TOKEN, form={"secret": SECRET})

        with self.assertRaises(Refused) as refused:
            sent(httpx2.ConnectError("[Errno 8] nodename nor servname provided"), call)

        self.assertIn(f"{API}/oauth", str(refused.exception))
        self.assertNotIn(TOKEN, str(refused.exception))
        self.assertNotIn(SECRET, str(refused.exception))

    def test_an_address_that_is_not_https_is_refused_before_any_request(self):
        with self.assertRaises(Refused):
            sent(httpx2.Response(200), Call("GET", "http://api.example.test/tokens", token=TOKEN))
