"""A capability call's body is read as strict JSON: a repeated field at any depth,
or a bare NaN or Infinity, is refused as JSON the API cannot read, before any
capability runs."""

from django.test import TestCase, override_settings

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class StrictBodyTests(TestCase):
    client_class = ContractClient

    def test_a_repeated_field_or_a_constant_is_refused_as_invalid_json(self):
        for attempt, body in enumerate(
            (
                '{"payload":{"name":"Example","name":"Other"}}',
                '{"payload":{"name":"Example"},"target":NaN}',
                '{"payload":{"name":"Example"},"target":-Infinity}',
            )
        ):
            with self.subTest(body=body), _serving():
                response = self.client.post(
                    "/api/v2/capabilities/project.create/",
                    data=body,
                    content_type="application/json",
                    HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                    HTTP_IDEMPOTENCY_KEY=f"strict-{attempt}",
                )

                self.assertEqual(response.status_code, 400, response.content)
                self.assertEqual(response.json()["error"]["code"], "invalid_json")

        from hq.domains.projects.models import Project

        self.assertFalse(Project.objects.filter(name__in=("Example", "Other")).exists())
