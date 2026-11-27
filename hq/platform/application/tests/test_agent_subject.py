"""One client two people signed in is two agents: what HQ keeps against an agent
(a rule, a block, a retry key, an allowance, a held ask) is kept against
``security.agent_subject``, so a retry key shows the subject is the person's.
Set through the product's own path, so it fails where HQ keys an agent by its
client alone.
"""

from django.conf import settings
from django.test import TestCase

from hq.domains.projects.models import Project
from hq.platform.core.testing import operator

from .. import roles
from ..capabilities import execute_capability
from ..linked_accounts import bind_sign_in, sign_in_subject
from ..tokens import token_principal

CLIENT = "example-assistant"
SCOPE = "read write_projects"


def _signed_in(subject: str):
    return token_principal({"client_id": CLIENT, "sub": subject, "scope": SCOPE}, "mcp")


class OneSubjectPerAgentAndPersonTests(TestCase):
    def test_bens_retry_of_annas_key_runs_as_ben_rather_than_replaying_annas_answer(self):
        for username, subject in (("anna", "example-anna-subject"), ("ben", "example-ben-subject")):
            user = operator(username=username)
            roles.assign(user, roles.Role.OPERATOR)
            bind_sign_in(user, sign_in_subject(settings.OIDC_ISSUER, subject))
        payload = {"name": "Shared", "idempotency_key": "example-key"}

        first = execute_capability("project.create", payload, principal=_signed_in("example-anna-subject"))
        second = execute_capability("project.create", payload, principal=_signed_in("example-ben-subject"))

        self.assertTrue(first["ok"] and second["ok"], (first, second))
        self.assertEqual(Project.objects.filter(name="Shared").count(), 2)
