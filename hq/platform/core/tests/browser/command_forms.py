"""A command's form in a real browser: what a press sends while the form says it is working."""

from typing import override
from urllib.parse import parse_qs

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client

from hq.domains.projects.models import Project
from hq.platform.application.action_links import command_url
from hq.platform.application.capabilities import capability_words
from hq.platform.application.projects import DEPENDENCY_UPDATES
from hq.platform.core.tests.browser.gate import BrowserGate, answered
from hq.platform.core.tests.browser.serving import ORIGIN

_DONE = "<!doctype html><html><head><title>Done</title></head><body><main>Done</main></body></html>"


class CommandFormBrowserTests(BrowserGate):
    """A switch is pressed as one of two buttons: the one pressed is what posts."""

    scripted = True
    path: str

    @classmethod
    @override
    def render(cls):
        cls.path = command_url(DEPENDENCY_UPDATES)
        with transaction.atomic():
            Project.objects.create(
                name="Example",
                slug="example",
                repository_url="https://github.com/example/example",
                dependency_updates=True,
            )
            client = Client()
            client.force_login(get_user_model().objects.create_superuser(username="operator"))
            page = answered(client.get(command_url(DEPENDENCY_UPDATES, "example")))
            transaction.set_rollback(True)
        return {cls.path.strip("/"): page}

    @override
    def start(self, **options):
        super().start(**options)
        self.posted = []

    @override
    def respond(self, route):
        if route.request.method == "POST":
            self.posted.append(parse_qs(route.request.post_data or ""))
            return self.fulfill(route, _DONE)
        return super().respond(route)

    def test_the_pressed_switch_posts_its_value_though_the_form_disables_it(self):
        self.page.goto(f"{ORIGIN}{command_url(DEPENDENCY_UPDATES, 'example')}", wait_until="load")

        self.page.get_by_role("button", name=capability_words(DEPENDENCY_UPDATES, "off")).click()
        self.page.wait_for_function("() => document.title === 'Done'")

        (posted,) = self.posted
        self.assertEqual(posted.get("dependency_updates"), ["false"])
        self.assertEqual(posted.get("__target"), ["example"])
