"""What the Agents page says of the person each agent acts for."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application import roles
from hq.platform.application.linked_accounts import bind_sign_in
from hq.platform.core.models import AgentIdentity
from hq.platform.core.testing import person

AGENT = "example-agent"
OTHER = "example-other"


def _seen(client_id: str, granted=("read",), person=None) -> None:
    AgentIdentity.objects.create(client_id=client_id, interfaces=["mcp"], granted=list(granted), person=person)


def _person(username: str, role: str = ""):
    """A person signed in as ``username``'s key, holding ``role`` (none when blank)."""

    user = person(username=username)
    bind_sign_in(user, username)
    if role:
        roles.assign(user, role)
    return user


class PageTests(TestCase):
    def test_each_agent_row_names_its_person_and_their_role(self):
        _seen(AGENT, person=_person("kim", roles.Role.VIEWER))
        _seen(OTHER, person=_person("nobody"))
        self.client.force_login(get_user_model().objects.create_user("operator"))

        page = self.client.get(reverse("agent_policy"))

        self.assertContains(page, 'data-role="viewer"')
        self.assertContains(page, "kim")
        self.assertContains(page, 'data-role="none"')
