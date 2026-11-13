"""Every setting a test overrides is one HQ declares.

``override_settings(NAME=...)`` and ``self.settings(NAME=...)`` accept any
name, so an override of a setting nothing declares, one removed or misspelled,
changes nothing and the test that relies on it passes for another reason.
"""

import tempfile
from pathlib import Path
from textwrap import dedent

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.apart import apart
from hq.platform.application.tests.reads import reads

from .setting_overrides import overridden

ROOT = Path(__file__).resolve().parents[4]


@tag("source", "tree")
@reads("**/*.py")
class SettingOverrideTests(SimpleTestCase):
    def test_every_overridden_setting_is_declared(self):
        found = apart(overridden, ROOT)

        self.assertIn("OIDC_ISSUER", found, "the walk read no override, so it checks nothing")
        self.assertEqual({name: where for name, where in found.items() if not hasattr(settings, name)}, {})

    def test_an_override_of_a_setting_nobody_declares_is_found(self):
        """The negative control: a walk that found nothing would pass the test above."""

        case = dedent(
            """
            from django.test import TestCase, override_settings

            API = override_settings(EXAMPLE_GONE=True, **{"EXAMPLE_SPREAD": 1})


            @override_settings(EXAMPLE_DECORATED=False)
            class Example(TestCase):
                def test_it(self):
                    with self.settings(EXAMPLE_INLINE=1):
                        pass
            """
        )
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "test_example.py").write_text(case, encoding="utf-8")

            found = apart(overridden, folder)

        self.assertEqual(sorted(found), ["EXAMPLE_DECORATED", "EXAMPLE_GONE", "EXAMPLE_INLINE", "EXAMPLE_SPREAD"])
