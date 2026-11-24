"""An unhandled error is kept under the request id its 500 page prints, and read there by an operator only."""

from unittest import mock

from django.apps import apps
from django.test import Client, TestCase, override_settings
from django.urls import path

from hq.config.urls import urlpatterns as site_urls
from hq.platform.application import roles
from hq.platform.application.markup_testing import elements, text, texts
from hq.platform.application.record_errors import RecordNotFound
from hq.platform.application.security import AuthorizationError, Principal, mcp_principal
from hq.platform.core.testing import READER, operator, person

handler500 = "hq.platform.core.error_views.server_error"


class ExampleFailure(RuntimeError):
    pass


def broken(request):
    raise ExampleFailure("The example view failed.")


urlpatterns = [path("example/broken/", broken), *site_urls]


def _kept():
    return apps.get_model("core", "ServerError").objects


def _get_resource(name, identifier, principal: Principal):
    from hq.platform.mcp import services
    from hq.platform.mcp.identity import set_principal

    with set_principal(principal):
        return services.get_resource(name, identifier)


@override_settings(ROOT_URLCONF=__name__)
class ServerErrorTests(TestCase):
    def setUp(self):
        self.client = Client(raise_request_exception=False)
        self.user = operator(self.client, username="example-operator")

    def _fail(self, client=None):
        response = (client or self.client).get("/example/broken/?token=example-secret")
        self.assertEqual(response.status_code, 500)
        (code,) = texts(response, "code")
        return response, code.rstrip(".")

    def test_an_unhandled_error_is_kept_under_the_id_its_page_prints(self):
        response, request_id = self._fail()

        kept = _kept().get()
        self.assertEqual(kept.request_id, request_id)
        self.assertEqual((kept.method, kept.path), ("GET", "/example/broken/"))
        self.assertEqual(kept.user, self.user)
        self.assertTrue(kept.exception.endswith("ExampleFailure"))
        self.assertIn("The example view failed.", kept.traceback)
        self.assertNotIn("example-secret", kept.traceback + kept.path)
        # The page links the error to an operator, who reads it there and over MCP.
        (link,) = (a.attrs["href"] for a in elements(response, "a") if a.words.rstrip(".") == request_id)
        page = self.client.get(link)
        self.assertEqual(page.status_code, 200)
        self.assertIn("The example view failed.", text(page))
        read = _get_resource("server_errors", request_id, mcp_principal())
        self.assertEqual(read["request_id"], request_id)
        self.assertIn("ExampleFailure", read["traceback"])

    def test_a_viewer_and_a_reader_agent_are_refused_the_error(self):
        _, request_id = self._fail()
        viewer = Client(raise_request_exception=False)
        roles.assign(person(viewer, username="example-viewer"), roles.Role.VIEWER)

        # The viewer's own 500 prints its id unlinked, and the kept error's page refuses them.
        viewed, own_id = self._fail(viewer)
        self.assertFalse([a for a in elements(viewed, "a") if a.words.rstrip(".") == own_id])
        self.assertEqual(viewer.get(f"/errors/{request_id}/").status_code, 403)
        self.assertEqual(viewer.get("/errors/").status_code, 403)
        with self.assertRaises(AuthorizationError):
            _get_resource("server_errors", request_id, READER)

    def test_an_error_that_cannot_be_kept_leaves_the_500_as_it_was(self):
        with mock.patch.object(
            apps.get_model("core", "ServerError"), "save", side_effect=RuntimeError("example store down")
        ):
            response, request_id = self._fail()

        self.assertIn(f"Its request id is {request_id}.", text(response))
        self.assertFalse(_kept().exists())
        with self.assertRaises(RecordNotFound):
            _get_resource("server_errors", request_id, mcp_principal())


class PruneTests(TestCase):
    def test_only_the_newest_kept_are_kept(self):
        from hq.platform.core.server_errors import KEPT, prune

        model = apps.get_model("core", "ServerError")
        model.objects.bulk_create(
            model(request_id=f"{n:032x}", exception="ExampleFailure", traceback="") for n in range(KEPT + 3)
        )

        prune()

        kept = set(model.objects.values_list("request_id", flat=True))
        self.assertEqual(kept, {f"{n:032x}" for n in range(3, KEPT + 3)})
