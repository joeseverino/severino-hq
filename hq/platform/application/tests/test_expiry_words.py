"""An expiry reads the same said from its moment or from the days left until it."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
# A future, the moment itself, earlier the same day, a day past and days past.
OFFSETS = (
    timedelta(days=3),
    timedelta(0),
    -timedelta(hours=2),
    -timedelta(days=4, hours=1),
    timedelta(hours=20),
    -timedelta(days=1, hours=2),
)


# The wording HQ and its controller both write, one case per line: the days
# left as ``days_until`` counts them, and the words for them. The controller's
# TestTLSConditionsWordAnExpiryAsHQDoes reads the same table.
CASES = Path(settings.BASE_DIR) / "controller/providers/testdata/expiry-words.json"


@tag("source")
class ExpiryWordsTests(SimpleTestCase):
    def test_each_count_of_days_reads_as_the_shared_table_says(self):
        from ..moments import expiry_words

        for case in json.loads(CASES.read_text(encoding="utf-8")):
            with self.subTest(days=case["days"]):
                self.assertEqual(expiry_words(case["days"]), case["words"])


class ExpiryMomentTests(SimpleTestCase):
    def test_a_count_of_days_reads_as_its_moment_does(self):
        from ..expiry import days_until
        from ..moments import expiry_words

        for offset in OFFSETS:
            when = NOW + offset
            with self.subTest(offset=offset):
                self.assertEqual(expiry_words(days_until(when, NOW)), expiry_words(when, NOW))
