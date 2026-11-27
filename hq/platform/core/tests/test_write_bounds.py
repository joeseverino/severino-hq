"""What a caller who has not signed in can make HQ write: every route and middleware that serves one says."""

from django.test import SimpleTestCase

from hq.platform.core import speculation
from hq.platform.core.write_bounds import declared


def undeclared_routes() -> list[str]:
    """Each route answering without a session whose view declares nothing it writes."""

    return sorted(route for route, view in speculation.sessionless_views() if not declared(view))


class DeclaredTests(SimpleTestCase):
    def test_every_route_that_answers_without_a_session_declares_what_it_writes(self):
        self.assertEqual(
            undeclared_routes(),
            [],
            "A route that answers without a session declares what it writes where it is defined: "
            "@writes(NOTHING), or @writes(<the Bound that holds it>) from hq.platform.core.write_bounds.",
        )
