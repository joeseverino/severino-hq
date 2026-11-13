"""The refused forms dev:look draws, each a POST by name (``post:<name>``):
what a reader sees after a form they sent was refused, its errors drawn, looked
at without a browser test that fills and sends it.

Each is posted by the estate's signed-in owner to the seed's estate as the
page's own form would send it, with a retry key of its own, and the page is
what the refusal answers. What it sends is written here or read from the
estate the seed built, never typed on the command line, so a name draws the
same state on every run.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.test import Client
from django.urls import reverse

lazy from hq.domains.projects.models import Project
lazy from hq.platform.application.plugin_testing import form_keyed
lazy from hq.platform.core.example_estate import REQUEST

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse

# How a POST's page is named among dev:look's pages.
POST = "post:"


@dataclass(frozen=True)
class Posted:
    """A form sent to ``route`` (reversed with ``args``) holding what ``data``
    returns, read once the estate is built, and refused with ``status``: an
    answer of any other status means the form no longer refuses it, and the
    look fails."""

    route: str
    args: tuple[str, ...]
    data: Callable[[], Mapping[str, Any]]
    status: int = 400

    def address(self) -> str:
        return reverse(self.route, args=self.args)

    def send(self, client: Client, **headers: str) -> _MonkeyPatchedWSGIResponse:
        """The form sent once by ``client``, as production answers it, a
        redirect followed, since a browser route cannot answer with one."""

        return client.post(self.address(), form_keyed(self.data()), secure=True, follow=True, **{**headers, **REQUEST})


def _a_project_held() -> Mapping[str, Any]:
    """A new project under the address of one the estate holds."""

    held = Project.objects.order_by("pk").first()
    if held is None:
        raise AssertionError("post:project-create-taken needs a seed that holds a project")
    return {"name": "Example taken address", "slug": held.slug}


POSTS = {
    # A command's form sent with nothing in it: each required field's error.
    "project-create-blank": Posted("command", ("project.create",), dict),
    # A command's form whose values pass the form and are refused by the
    # command itself: the refusal said over the form as sent.
    "project-create-taken": Posted("command", ("project.create",), _a_project_held),
}


def is_post(name: str) -> bool:
    return name.startswith(POST)


def posted(name: str) -> Posted:
    return POSTS[name.removeprefix(POST)]


def post_names() -> list[str]:
    """Every POST by the name dev:look takes."""

    return [f"{POST}{name}" for name in POSTS]
