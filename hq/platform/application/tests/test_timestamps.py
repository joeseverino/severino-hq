"""The one timestamp parser, edge by edge, and the call sites it corrected.

The parser's own promises are properties: Hypothesis writes the stamps, as
many as the run's profile asks for (``hypothesis_profiles`` in the test
runner). It is a development tool the image lacks, so there they are skipped.
"""

from datetime import UTC, date, datetime, time, timedelta, timezone
from unittest import mock, skipUnless

from django.test import SimpleTestCase

from hq.platform.application.tests.markup import text

from ..timestamps import moment

try:
    from hypothesis import example, given, strategies as st
except ImportError:  # pragma: no cover - the image does not carry it
    HYPOTHESIS = False
else:
    HYPOTHESIS = True

if HYPOTHESIS:
    NAIVE_MODES = st.sampled_from(("utc", "keep", "refuse"))
    # Any offset a stamp can carry, to the microsecond Python writes.
    OFFSET = st.timedeltas(min_value=-timedelta(hours=23, minutes=59), max_value=timedelta(hours=23, minutes=59)).map(
        timezone
    )
    # Go's zero day is the one day ``moment`` reads as never, so an instant is
    # any time after it.
    NAIVE = st.datetimes(min_value=datetime(1, 1, 2))
    PADDING = st.text(" \t\n\r", max_size=3)


def _written(instant: datetime, *, z: bool) -> str:
    """``instant`` as a provider writes it, with a trailing Z for UTC when ``z``."""

    written = instant.isoformat()
    return written.removesuffix("+00:00") + "Z" if z and written.endswith("+00:00") else written


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class MomentProperties(SimpleTestCase):
    if HYPOTHESIS:

        @given(NAIVE, OFFSET, NAIVE_MODES, PADDING, PADDING, st.booleans())
        def test_an_aware_stamp_round_trips(self, naive, offset, mode, before, after, z):
            instant = naive.replace(tzinfo=offset)

            read = moment(f"{before}{_written(instant, z=z)}{after}", naive=mode)

            self.assertEqual((read, read.utcoffset()), (instant, instant.utcoffset()))
            self.assertIs(moment(instant, naive=mode), instant)

        @given(NAIVE, PADDING, PADDING)
        def test_a_naive_stamp_is_utc_kept_or_refused_as_asked(self, naive, before, after):
            for stamp in (f"{before}{naive.isoformat()}{after}", naive):
                self.assertEqual(moment(stamp), naive.replace(tzinfo=UTC))
                self.assertEqual(moment(stamp, naive="keep"), naive)
                self.assertIsNone(moment(stamp, naive="refuse"))
            self.assertIs(moment(naive, naive="keep"), naive)

        @given(clock=st.times(), offset=st.none() | OFFSET, mode=NAIVE_MODES, as_text=st.booleans())
        @example(clock=time(), offset=UTC, mode="utc", as_text=True)
        @example(clock=time(), offset=None, mode="keep", as_text=True)
        def test_the_zero_day_means_never_unless_asked(self, clock, offset, mode, as_text):
            instant = datetime.combine(date.min, clock, tzinfo=offset)
            stamp = _written(instant, z=True) if as_text else instant

            self.assertIsNone(moment(stamp, naive=mode))
            if offset is not None:
                self.assertEqual(moment(stamp, naive=mode, zero_is_never=False), instant)

        @given(st.dates(min_value=date(1, 1, 2)))
        def test_a_date_reads_as_its_midnight(self, day):
            self.assertEqual(moment(day), datetime.combine(day, time(), tzinfo=UTC))

        @given(
            value=st.one_of(st.text(), st.none(), st.integers(), st.floats(), st.binary(), st.lists(st.integers())),
            mode=NAIVE_MODES,
            zero_is_never=st.booleans(),
        )
        @example(value="", mode="utc", zero_is_never=True)
        @example(value="   ", mode="utc", zero_is_never=True)
        @example(value="soon", mode="utc", zero_is_never=True)
        @example(value="None", mode="utc", zero_is_never=True)
        @example(value="2026-13-01T00:00:00Z", mode="utc", zero_is_never=True)
        @example(value=0, mode="utc", zero_is_never=True)
        @example(value=[], mode="utc", zero_is_never=True)
        def test_any_value_is_an_instant_or_none(self, value, mode, zero_is_never):
            """Never raises; text without a digit names no instant, and an answer
            it was not asked to keep naive carries an offset."""

            read = moment(value, naive=mode, zero_is_never=zero_is_never)

            if not any(character.isdigit() for character in str(value or "")):
                self.assertIsNone(read)
            if read is not None:
                self.assertIsInstance(read, datetime)
                self.assertTrue(mode == "keep" or read.tzinfo is not None, read)


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

        with (
            mock.patch.object(github, "get", return_value={"pushed_at": 12345}),
            self.assertRaises(github.GitHubMetadataError),
        ):
            github.fetch_last_push("https://github.com/example/example")
        with mock.patch.object(github, "get", return_value={"pushed_at": "2026-07-25T10:00:00Z"}):
            self.assertEqual(
                github.fetch_last_push("https://github.com/example/example"),
                datetime(2026, 7, 25, 10, tzinfo=UTC),
            )

    def test_a_malformed_expiry_phrase_echoes_the_provider(self):
        from hq.domains.control_plane.provider_spec import expiry_phrase

        self.assertEqual(expiry_phrase("soon"), "soon")
        self.assertEqual(expiry_phrase(None), "")
        self.assertEqual(expiry_phrase("0001-01-01T00:00:00Z"), "0001-01-01T00:00:00Z")

    def test_readable_leaves_what_it_cannot_parse(self):
        from hq.platform.core.templatetags.value_tags import readable

        self.assertEqual(readable("nope T"), "nope T")
        self.assertEqual(text(readable("0001-01-01T00:00:00")), "0001-01-01T00:00:00")
        self.assertNotEqual(readable("2026-07-25T10:00:00Z"), "2026-07-25T10:00:00Z")

    def test_content_dates_ignore_the_zero_time(self):
        from hq.domains.content.content_sync import _parse_date

        self.assertEqual(_parse_date("2026-07-25T23:30:00Z"), date(2026, 7, 25))
        self.assertIsNone(_parse_date("0001-01-01T00:00:00Z"))
        self.assertIsNone(_parse_date(None))
