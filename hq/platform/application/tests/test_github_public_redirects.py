"""A GitHub read follows the redirect GitHub answers for a renamed repository."""

from django.test import SimpleTestCase

from .. import github_public
from .network import Response, answering, moved


class RedirectTests(SimpleTestCase):
    def test_a_renamed_repository_is_followed(self):
        asked = []

        def github(request):
            asked.append(request)
            return (
                moved("https://api.github.com/repositories/1")
                if len(asked) == 1
                else Response({"full_name": "example/app"})
            )

        with answering(github):
            found = github_public.get("/repos/example/old-name")

        self.assertEqual(found, {"full_name": "example/app"})
        self.assertEqual(str(asked[1].url), "https://api.github.com/repositories/1")
