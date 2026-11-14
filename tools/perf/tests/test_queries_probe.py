"""The query probe tells a page's own queries from those it repeats for each row it draws."""

import io
import json
import os
from contextlib import redirect_stdout
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.commands import shell
from django.http import HttpResponse
from django.test import TestCase, override_settings, tag
from django.urls import include, path

from tools.perf import targets


def _rows(_request, records: int) -> HttpResponse:
    """A page that counts its records once, then reads each record it draws on its own: N+1."""

    users = get_user_model()._default_manager
    users.count()
    for _ in range(records):
        users.filter(pk=-1).exists()
    return HttpResponse("rows")


# Beside the real routes, which the signed-in middleware reverses.
urlpatterns = [path("example/rows/<int:records>/", _rows), path("", include(settings.ROOT_URLCONF))]


@tag("source")
@override_settings(ROOT_URLCONF=__name__)
class ProbeTests(TestCase):
    def counted(self, *pages: str) -> dict:
        users = get_user_model()
        users._default_manager.create(**{users.USERNAME_FIELD: "example-operator"})
        env = {"PERF_USER": "example-operator", "PERF_PAGES": json.dumps(pages)}
        printed = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(printed):
            # Run as `manage.py shell -c` runs it, in the shell command module's globals.
            code = compile(targets.PROBE.read_text(encoding="utf-8"), str(targets.PROBE), "exec")
            exec(code, {"__name__": shell.__name__})  # noqa: S102 the probe is a script, never imported
        return targets.probe_counts(printed.getvalue())

    def test_only_the_repeated_queries_grow_with_the_rows_a_page_draws(self):
        counts = self.counted("/example/rows/2/", "/example/rows/5/")
        two, five = counts["/example/rows/2/"], counts["/example/rows/5/"]

        self.assertEqual(five["repeated_queries"] - two["repeated_queries"], 3)
        self.assertEqual(
            five["queries"] - five["repeated_queries"], two["queries"] - two["repeated_queries"], "the page's own"
        )

    def test_a_page_that_does_not_answer_200_has_no_counts(self):
        self.assertIsNone(self.counted("/example/missing/")["/example/missing/"])
