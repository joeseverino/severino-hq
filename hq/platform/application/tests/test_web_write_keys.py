"""Every web write carries a retry key, so a form sent twice acts once: file_rules holds every form that posts
to ``{% write_token %}``, and the runner refuses a change the browser sends without one."""

from django.test import TestCase

from hq.platform.core.testing import operator

from ..plugin_testing import form_keyed
from ..web_writes import RETRY_KEY_FIELD
from .writers import drawn_key


class RedrawnFormTests(TestCase):
    def test_a_form_drawn_again_after_its_write_draws_a_fresh_key(self):

        operator(self.client)
        posted = form_keyed({"name": "Example", "slug": "example-redrawn"})

        first = self.client.post("/commands/project.create/", posted, follow=True)
        again = self.client.get("/commands/project.create/")

        self.assertEqual(first.status_code, 200)
        self.assertNotEqual(drawn_key(again, "data-command-form"), posted[RETRY_KEY_FIELD])
