"""One way to write a moment, a date and an age, and one filter that shows them."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import formats, timezone

from ..moments import ago, when, when_day, when_exact, when_range
from ..ui import MISSING

CHICAGO = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def this_year(month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(timezone.localdate().year, month, day, hour, minute, second, tzinfo=CHICAGO)


@override_settings(TIME_ZONE="America/Chicago")
class PhrasingTests(SimpleTestCase):
    def test_a_moment_is_a_day_and_a_twelve_hour_clock(self):
        self.assertEqual(when(this_year(10, 3, 9, 29)), "Oct 3, 9:29 AM")
        self.assertEqual(when(this_year(10, 3, 22, 33)), "Oct 3, 10:33 PM")
        self.assertEqual(when(this_year(1, 9, 0, 5)), "Jan 9, 12:05 AM")

    def test_the_year_is_said_only_when_it_is_not_this_one(self):
        self.assertEqual(when(datetime(2019, 10, 3, 9, 29, tzinfo=CHICAGO)), "Oct 3, 2019, 9:29 AM")
        self.assertEqual(when_day(date(2019, 10, 3)), "Oct 3, 2019")
        self.assertEqual(when_day(date(timezone.localdate().year, 10, 3)), "Oct 3")

    def test_a_moment_is_read_in_the_timezone_hq_is_read_in(self):
        late = datetime(timezone.localdate().year, 7, 4, 3, 0, tzinfo=UTC)

        self.assertEqual(when(late), "Jul 3, 10:00 PM")
        self.assertEqual(when_day(late), "Jul 3")

    def test_a_bare_date_has_no_time_of_day(self):
        self.assertEqual(when(date(2019, 10, 3)), "Oct 3, 2019")
        self.assertEqual(when_exact(date(2019, 10, 3)), "Oct 3, 2019")

    def test_the_exact_form_always_says_the_year_the_seconds_and_the_zone(self):
        self.assertEqual(when_exact(this_year(10, 3, 9, 29, 15)), f"Oct 3, {timezone.localdate().year}, 9:29:15 AM CDT")
        self.assertEqual(when_exact(datetime(2019, 1, 3, 9, 29, 15)), "Jan 3, 2019, 9:29:15 AM")

    def test_an_age_is_one_unit(self):
        now = timezone.now()

        self.assertEqual(ago(now - timedelta(days=6, hours=12)), "6\xa0days ago")
        self.assertEqual(ago(now - timedelta(hours=3, minutes=40)), "3\xa0hours ago")
        self.assertEqual(ago(now - timedelta(seconds=20)), "just now")

    def test_a_run_of_days_says_a_shared_year_once(self):
        year = timezone.localdate().year

        self.assertEqual(when_range(date(year, 10, 3), date(year, 10, 9)), "Oct 3 – Oct 9")
        self.assertEqual(when_range(date(2019, 10, 3), date(2019, 11, 2)), "Oct 3 – Nov 2, 2019")
        self.assertEqual(when_range(date(2018, 12, 29), date(2019, 1, 4)), "Dec 29, 2018 – Jan 4, 2019")
        self.assertEqual(when_range(date(2019, 10, 3), date(2019, 10, 3)), "Oct 3, 2019")

    def test_a_run_of_days_is_read_in_the_timezone_hq_is_read_in(self):
        year = timezone.localdate().year
        late = datetime(year, 7, 4, 3, 0, tzinfo=UTC)

        self.assertEqual(when_range(late, datetime(year, 7, 9, 12, 0, tzinfo=UTC)), "Jul 3 – Jul 9")

    def test_a_date_printed_bare_reads_the_same_way(self):
        """Django's own formats are the owner's, so a template that forgot the filter agrees."""

        self.assertEqual(formats.date_format(date(2019, 10, 3)), "Oct 3, 2019")
        self.assertEqual(
            formats.date_format(datetime(2019, 10, 3, 9, 29), "DATETIME_FORMAT"), "Oct 3, 2019, 9:29 AM"
        )


@override_settings(TIME_ZONE="America/Chicago")
class WhenFilterTests(SimpleTestCase):
    def render(self, value, form=None):
        source = "{{ value|when }}" if form is None else '{{ value|when:"%s" }}' % form
        return Template(source).render(Context({"value": value}))

    def test_a_moment_is_a_time_element_a_table_can_sort_on(self):
        moment = this_year(10, 3, 9, 29, 15)

        self.assertEqual(
            self.render(moment),
            f'<time datetime="{moment.isoformat()}" title="{when_exact(moment)}">Oct 3, 9:29 AM</time>',
        )

    def test_an_age_keeps_the_exact_moment_behind_it(self):
        moment = timezone.now() - timedelta(days=5, hours=15)

        self.assertEqual(
            self.render(moment, "ago"),
            f'<time datetime="{moment.isoformat()}" title="{when_exact(moment)}">5\xa0days ago</time>',
        )

    def test_a_date_is_a_date(self):
        self.assertEqual(
            self.render(date(2019, 10, 3)),
            '<time datetime="2019-10-03" title="Oct 3, 2019">Oct 3, 2019</time>',
        )
        self.assertIn(">Oct 3, 2019</time>", self.render(date(2019, 10, 3), "ago"))

    def test_the_day_and_exact_forms(self):
        moment = datetime(2019, 10, 3, 9, 29, 15, tzinfo=CHICAGO)

        self.assertIn(">Oct 3, 2019</time>", self.render(moment, "day"))
        self.assertIn(">Oct 3, 2019, 9:29:15 AM CDT</time>", self.render(moment, "exact"))

    def test_an_iso_stamp_reads_like_the_instant_it_names(self):
        moment = this_year(10, 3, 9, 29, 15)

        self.assertEqual(self.render(moment.isoformat()), self.render(moment))
        self.assertIn('<time datetime="2019-10-03"', self.render("2019-10-03"))

    def test_nothing_is_the_missing_mark_and_other_text_is_left_alone(self):
        self.assertIn(MISSING, self.render(None))
        self.assertIn('class="empty-value"', self.render(""))
        self.assertEqual(self.render("Tailnet"), "Tailnet")

    def test_an_unknown_form_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            self.render(timezone.now(), "short")


class OneOwnerTests(SimpleTestCase):
    """A page asks ``|when``; it does not spell a clock of its own."""

    root = Path(__file__).resolve().parent.parent.parent

    def test_no_template_formats_a_time_of_day_itself(self):
        clock = re.compile(r"""\|\s*date:\s*["'][^"']*[gGhHisaA][^"']*["']""")
        templates = sorted((self.root / "templates").rglob("*.html"))
        self.assertTrue(templates)

        spelled = [
            f"{path.relative_to(self.root)}: {found.group(0)}"
            for path in templates
            for found in clock.finditer(path.read_text())
        ]

        self.assertEqual(spelled, [])

    def test_django_is_handed_the_owners_formats(self):
        from config.formats.en import formats as handed

        from .. import moments

        self.assertEqual(handed.DATE_FORMAT, moments.DAY_YEAR_FORMAT)
        self.assertEqual(handed.DATETIME_FORMAT, moments.MOMENT_YEAR_FORMAT)
        self.assertEqual(handed.TIME_FORMAT, moments.CLOCK_FORMAT)


class DaySpanTests(TestCase):
    """A run of local days, as the moments a datetime column is filtered by."""

    def test_a_run_of_days_is_midnight_to_the_midnight_after_the_last(self):
        from datetime import date

        from django.utils import timezone

        from ..projection import day_span

        start, end = day_span(date(2026, 3, 1), date(2026, 3, 2))

        self.assertEqual(timezone.localtime(start).isoformat()[:16], "2026-03-01T00:00")
        self.assertEqual(timezone.localtime(end).isoformat()[:16], "2026-03-03T00:00")
        self.assertTrue(timezone.is_aware(start))

    def test_from_a_day_on_has_no_end_and_a_moment_counts_as_its_local_day(self):
        from datetime import date, datetime, timezone as tz

        from django.utils import timezone

        from ..projection import day_span

        self.assertIsNone(day_span(date(2026, 3, 1))[1])
        late = datetime(2026, 3, 2, 3, 0, tzinfo=tz.utc)
        self.assertEqual(day_span(late)[0], day_span(timezone.localtime(late).date())[0])

    def test_it_matches_what_a_date_cast_would_have_kept(self):
        from datetime import date, datetime, timedelta, timezone as tz

        from core.models import AuditLog

        from ..projection import day_span

        day = date(2026, 3, 1)
        for hours in range(-30, 54, 3):
            row = AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Example")
            AuditLog.objects.filter(pk=row.pk).update(
                created_at=datetime(2026, 3, 1, 12, tzinfo=tz.utc) + timedelta(hours=hours)
            )
        start, end = day_span(day, day)

        self.assertEqual(
            set(AuditLog.objects.filter(created_at__gte=start, created_at__lt=end).values_list("pk", flat=True)),
            set(AuditLog.objects.filter(created_at__date=day).values_list("pk", flat=True)),
        )


class SpanTests(SimpleTestCase):
    def test_a_deadline_is_counted_in_days_and_a_long_one_in_months_and_years(self):
        from ..moments import span

        self.assertEqual(span(1), "1 day")
        self.assertEqual(span(46), "46 days")
        self.assertEqual(span(90), "90 days")
        self.assertEqual(span(301), "9 months, 3 weeks")
        self.assertEqual(span(1443), "3 years, 11 months")
        self.assertEqual(span(-3), "3 days overdue")
