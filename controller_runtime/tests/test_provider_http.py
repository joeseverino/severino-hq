"""The one way the controller sends a provider request."""

from __future__ import annotations

from django.test import SimpleTestCase

from ..providers import ProviderError


class BoundedResponseTests(SimpleTestCase):
    def test_an_answer_past_the_cap_is_refused_not_held(self):
        from unittest import mock

        from .. import provider_http

        class Endless:
            def read(self, amount=-1):
                return b"x" * (amount if amount and amount > 0 else 10)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with (
            mock.patch.object(provider_http, "MAX_RESPONSE_BYTES", 1024),
            mock.patch("urllib.request.urlopen", return_value=Endless()),
        ):
            with provider_http.open_url("https://provider.example") as response:
                with self.assertRaisesRegex(ProviderError, "more than"):
                    response.read()
                    response.read()
