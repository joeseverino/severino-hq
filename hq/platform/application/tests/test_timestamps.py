"""The one timestamp parser, edge by edge, and the call sites it corrected."""

import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from ..timestamps import moment

UTC = timezone.utc
PLUS_TWO = timezone(timedelta(hours=2))
ROOT = Path(__file__).resolve().parents[4]


class MomentTests(SimpleTestCase):
    def test_an_aware_stamp_keeps_its_own_offset(self):
        for naive in ("utc", "keep", "refuse"):
            with self.subTest(naive=naive):
                self.assertEqual(
                    moment("2026-07-25T10:00:00+02:00", naive=naive),
                    datetime(2026, 7, 25, 10, tzinfo=PLUS_TWO),
                )

    def test_a_trailing_z_is_utc(self):
        self.assertEqual(
            moment("2026-07-25T10:00:00Z"), datetime(2026, 7, 25, 10, tzinfo=UTC)
        )

    def test_naive_is_read_as_utc_by_default(self):
        self.assertEqual(
            moment("2026-07-25T10:00:00"), datetime(2026, 7, 25, 10, tzinfo=UTC)
        )

    def test_naive_can_be_kept_or_refused(self):
        self.assertEqual(
            moment("2026-07-25T10:00:00", naive="keep"), datetime(2026, 7, 25, 10)
        )
        self.assertIsNone(moment("2026-07-25T10:00:00", naive="refuse"))

    def test_the_zero_time_means_never(self):
        for stamp in (
            "0001-01-01T00:00:00Z",
            "0001-01-01T00:00:00",
            datetime.min,
            datetime.min.replace(tzinfo=UTC),
        ):
            with self.subTest(stamp=stamp):
                self.assertIsNone(moment(stamp))
                self.assertIsNone(moment(stamp, naive="keep"))

    def test_the_zero_time_can_be_read_as_an_instant(self):
        self.assertEqual(
            moment("0001-01-01T00:00:00Z", zero_is_never=False),
            datetime.min.replace(tzinfo=UTC),
        )

    def test_nothing_and_nonsense_are_none(self):
        for stamp in (None, "", "   ", "soon", "2026-13-01T00:00:00Z", 0, [], "None"):
            with self.subTest(stamp=stamp):
                self.assertIsNone(moment(stamp))

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(
            moment(" 2026-07-25T10:00:00Z\n"), datetime(2026, 7, 25, 10, tzinfo=UTC)
        )

    def test_a_datetime_is_taken_as_given_and_held_to_the_same_rules(self):
        aware = datetime(2026, 7, 25, 10, tzinfo=PLUS_TWO)
        naive = datetime(2026, 7, 25, 10)
        self.assertIs(moment(aware), aware)
        self.assertIs(moment(naive, naive="keep"), naive)
        self.assertEqual(moment(naive), naive.replace(tzinfo=UTC))
        self.assertIsNone(moment(naive, naive="refuse"))

    def test_a_date_reads_as_its_midnight(self):
        self.assertEqual(moment(date(2026, 7, 25)), datetime(2026, 7, 25, tzinfo=UTC))

    def test_extensions_are_handed_the_one_parser(self):
        from .. import timestamps

        self.assertIs(timestamps.moment, moment)


class CorrectedCallSiteTests(SimpleTestCase):
    """Call sites held to the correct answer at their edges."""

    def test_a_tailnet_key_at_the_zero_time_has_no_expiry(self):
        from ..tailnet_presence import Presence

        self.assertIsNone(Presence(key_expires="0001-01-01T00:00:00Z").key_expiry_days)
        self.assertIsNone(Presence(key_expires="2026-13-01T00:00:00Z").key_expiry_days)
        soon = (datetime.now(UTC) + timedelta(days=10)).isoformat()
        self.assertEqual(Presence(key_expires=soon).key_expiry_days, 10)

    def test_a_registration_expiry_keeps_its_offset(self):
        """An offset is kept, not overwritten with UTC, so the instant does not
        move.
        """

        from ..expiry import days_until

        when = moment("2026-07-25T23:00:00-05:00")
        self.assertEqual(when, datetime(2026, 7, 26, 4, tzinfo=UTC))
        now = datetime(2026, 7, 25, 12, tzinfo=UTC)
        self.assertEqual(days_until(when, now), 1)

    def test_a_non_text_pushed_at_is_a_metadata_error_not_a_crash(self):
        from hq.domains.projects import github

        with mock.patch.object(github, "get", return_value={"pushed_at": 12345}):
            with self.assertRaises(github.GitHubMetadataError):
                github.fetch_last_push("https://github.com/example/example")
        with mock.patch.object(
            github, "get", return_value={"pushed_at": "2026-07-25T10:00:00Z"}
        ):
            self.assertEqual(
                github.fetch_last_push("https://github.com/example/example"),
                datetime(2026, 7, 25, 10, tzinfo=UTC),
            )

    def test_a_malformed_expiry_phrase_echoes_the_provider(self):
        from hq.domains.control_plane.provider_spec import expiry_phrase

        self.assertEqual(expiry_phrase("soon"), "soon")
        self.assertEqual(expiry_phrase(None), "")
        self.assertEqual(
            expiry_phrase("0001-01-01T00:00:00Z"), "0001-01-01T00:00:00Z"
        )

    def test_readable_leaves_what_it_cannot_parse(self):
        from hq.platform.core.templatetags.value_tags import readable

        self.assertEqual(readable("nope T"), "nope T")
        self.assertEqual(readable("0001-01-01T00:00:00"), "0001-01-01T00:00:00")
        self.assertNotEqual(readable("2026-07-25T10:00:00Z"), "2026-07-25T10:00:00Z")

    def test_content_dates_ignore_the_zero_time(self):
        from hq.domains.content.content_sync import _parse_date

        self.assertEqual(_parse_date("2026-07-25T23:30:00Z"), date(2026, 7, 25))
        self.assertIsNone(_parse_date("0001-01-01T00:00:00Z"))
        self.assertIsNone(_parse_date(None))


class OneParserTests(SimpleTestCase):
    """A new ``datetime.fromisoformat`` outside the kept list is a new copy."""

    # Kept on purpose; each has an edge ``moment`` does not share. See the
    # module docstring of ``application/timestamps.py`` and the PR that
    # introduced it.
    KEPT = {
        "hq/platform/application/timestamps.py",
        "hq/domains/docs_index/importer.py",
    }

    def test_no_other_module_parses_an_iso_instant_itself(self):
        offenders = []
        tops = [
            top
            for top in ROOT.iterdir()
            if top.is_dir() and not top.is_symlink() and not top.name.startswith(".")
        ]
        for path in sorted(path for top in tops for path in top.rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            if (
                relative in self.KEPT
                or path.name.startswith("test")
                or "/migrations/" in relative
            ):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "fromisoformat"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "datetime"
                ):
                    offenders.append(f"{relative}:{node.lineno}")
        self.assertEqual(offenders, [])
