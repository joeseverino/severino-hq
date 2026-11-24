"""The pinned tools' audit ignores what osv-scanner.toml ignores, and only until its expiry."""

import datetime

from django.test import SimpleTestCase

from tools.pipeline import tool_advisories

DAY = datetime.date(2027, 1, 17)


def advisory(id_, aliases=()):
    # The shape `mise audit --json` prints for each advisory.
    return {
        "tool": "gitleaks",
        "version": "8.30.1",
        "id": id_,
        "aliases": list(aliases),
        "severity": None,
        "summary": "A summary",
        "fixed": [],
        "url": f"https://osv.dev/vulnerability/{id_}",
    }


class IgnoreTests(SimpleTestCase):
    def left(self, found, entry, today):
        skip = tool_advisories.ignored({"IgnoredVulns": [entry]}, today)
        return tool_advisories.unignored({"vulnerabilities": [found], "unchecked": []}, skip)

    def test_an_ignored_advisory_passes_until_its_expiry_and_fails_from_that_day(self):
        entry = {"id": "CVE-2026-1", "effectiveUntil": DAY, "reason": "Not reached."}
        for found in (advisory("CVE-2026-1"), advisory("GHSA-aaaa-bbbb-cccc", ["CVE-2026-1"])):
            with self.subTest(id=found["id"]):
                self.assertEqual(self.left(found, entry, DAY - datetime.timedelta(days=1)), [])
                self.assertEqual(self.left(found, entry, DAY), [found])

    def test_an_advisory_no_entry_names_fails(self):
        found = advisory("CVE-2026-2")
        self.assertEqual(self.left(found, {"id": "CVE-2026-1", "reason": "Not reached."}, DAY), [found])
