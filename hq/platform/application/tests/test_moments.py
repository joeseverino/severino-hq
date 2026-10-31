"""One way to write a moment, a date and an age, and one filter that shows them."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import formats, timezone

from ..moments import (
    ago,
    ahead,
    length,
    month,
    numeral,
    weekday,
    weekday_initials,
    when,
    when_day,
    when_exact,
    when_range,
)
from ..ui import MISSING
from .markup import elements, text, with_class

CHICAGO = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")


def this_year(month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(timezone.localdate().year, month, day, hour, minute, second, tzinfo=CHICAGO)


@override_settings(TIME_ZONE="America/Chicago")
class PhrasingTests(SimpleTestCase):
    def test_a_moment_is_a_day_and_a_twelve_hour_clock(self):
        self.assertEqual(when(this_year(10, 3, 9, 29)), "Oct\xa03, 9:29\xa0AM")
        self.assertEqual(when(this_year(10, 3, 22, 33)), "Oct\xa03, 10:33\xa0PM")
        self.assertEqual(when(this_year(1, 9, 0, 5)), "Jan\xa09, 12:05\xa0AM")

    def test_the_year_is_said_only_when_it_is_not_this_one(self):
        self.assertEqual(when(datetime(2019, 10, 3, 9, 29, tzinfo=CHICAGO)), "Oct\xa03,\xa02019, 9:29\xa0AM")
        self.assertEqual(when_day(date(2019, 10, 3)), "Oct\xa03,\xa02019")
        self.assertEqual(when_day(date(timezone.localdate().year, 10, 3)), "Oct\xa03")

    def test_a_moment_is_read_in_the_timezone_hq_is_read_in(self):
        late = datetime(timezone.localdate().year, 7, 4, 3, 0, tzinfo=UTC)

        self.assertEqual(when(late), "Jul\xa03, 10:00\xa0PM")
        self.assertEqual(when_day(late), "Jul\xa03")

    def test_a_bare_date_has_no_time_of_day(self):
        self.assertEqual(when(date(2019, 10, 3)), "Oct\xa03,\xa02019")
        self.assertEqual(when_exact(date(2019, 10, 3)), "Oct\xa03,\xa02019")

    def test_the_exact_form_always_says_the_year_the_seconds_and_the_zone(self):
        self.assertEqual(
            when_exact(this_year(10, 3, 9, 29, 15)), f"Oct\xa03,\xa0{timezone.localdate().year}, 9:29:15\xa0AM\xa0CDT"
        )
        naive = datetime(2019, 1, 3, 9, 29, 15)  # noqa: DTZ001 - a naive moment is shown without a zone
        self.assertEqual(when_exact(naive), "Jan\xa03,\xa02019, 9:29:15\xa0AM")

    def test_an_age_is_one_unit(self):
        now = timezone.now()

        self.assertEqual(ago(now - timedelta(days=6, hours=12)), "6\xa0days ago")
        self.assertEqual(ago(now - timedelta(hours=3, minutes=40)), "3\xa0hours ago")
        self.assertEqual(ago(now - timedelta(seconds=20)), "just now")

    def test_a_run_of_days_says_a_shared_year_once(self):
        year = timezone.localdate().year

        self.assertEqual(when_range(date(year, 10, 3), date(year, 10, 9)), "Oct\xa03 – Oct\xa09")
        self.assertEqual(when_range(date(2019, 10, 3), date(2019, 11, 2)), "Oct\xa03 – Nov\xa02,\xa02019")
        self.assertEqual(when_range(date(2018, 12, 29), date(2019, 1, 4)), "Dec\xa029,\xa02018 – Jan\xa04,\xa02019")
        self.assertEqual(when_range(date(2019, 10, 3), date(2019, 10, 3)), "Oct\xa03,\xa02019")

    def test_a_run_of_days_is_read_in_the_timezone_hq_is_read_in(self):
        year = timezone.localdate().year
        late = datetime(year, 7, 4, 3, 0, tzinfo=UTC)

        self.assertEqual(when_range(late, datetime(year, 7, 9, 12, 0, tzinfo=UTC)), "Jul\xa03 – Jul\xa09")

    def test_a_calendar_names_its_weekdays_months_and_days(self):
        late = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)

        self.assertEqual(weekday(date(2026, 10, 5)), "Monday")
        self.assertEqual(weekday(date(2026, 10, 5), short=True), "Mon")
        self.assertEqual(weekday_initials(date(2026, 10, 6)), "Tu")
        self.assertEqual(weekday_initials(date(2026, 10, 8)), "Th")
        self.assertEqual(month(date(2026, 10, 5)), "October\xa02026")
        self.assertEqual(month(date(2026, 10, 5), short=True), "Oct")
        self.assertEqual(numeral(date(2026, 10, 5)), "5")
        # Each is the day the moment falls on here, a day before it does in UTC.
        self.assertEqual((weekday(late), numeral(late)), ("Sunday", "4"))
        self.assertEqual(month(datetime(2026, 11, 1, 3, 0, tzinfo=UTC)), "October\xa02026")

    def test_a_length_is_one_unit_either_way(self):
        now = timezone.now()

        self.assertEqual(length(now - timedelta(hours=3, minutes=40), now), "3\xa0hours")
        self.assertEqual(length(now + timedelta(minutes=12, seconds=30), now), "12\xa0minutes")
        self.assertEqual(length(now + timedelta(seconds=20), now), "under a minute")
        self.assertEqual(length(date(2019, 10, 3)), "Oct\xa03,\xa02019")

    def test_what_is_ahead_says_how_soon_and_what_has_passed_says_how_long_ago(self):
        now = timezone.now()

        self.assertEqual(ahead(now + timedelta(days=3, hours=2), now), "in 3\xa0days")
        self.assertEqual(ahead(now + timedelta(seconds=20), now), "in under a minute")
        self.assertEqual(ahead(now - timedelta(days=5, hours=2), now), "5\xa0days ago")
        self.assertEqual(ahead(date(2019, 10, 3)), "Oct\xa03,\xa02019")

    def test_a_date_printed_bare_reads_the_same_way(self):
        """Django's own formats are the owner's, so a template that forgot the filter agrees."""

        self.assertEqual(formats.date_format(date(2019, 10, 3)), "Oct\xa03,\xa02019")
        naive = datetime(2019, 10, 3, 9, 29)  # noqa: DTZ001 - the format alone is under test
        self.assertEqual(formats.date_format(naive, "DATETIME_FORMAT"), "Oct\xa03,\xa02019, 9:29\xa0AM")


@override_settings(TIME_ZONE="America/Chicago")
class WhenFilterTests(SimpleTestCase):
    def render(self, value, form=None):
        source = "{{ value|when }}" if form is None else f'{{{{ value|when:"{form}" }}}}'
        return Template(source).render(Context({"value": value}))

    def moment(self, value, form=None) -> tuple[str, str, str]:
        """The one time element drawn: what it sorts on, what it says on hover, and its words."""

        (drawn,) = elements(self.render(value, form), "time")
        return drawn.attrs["datetime"], drawn.attrs.get("title", ""), drawn.text

    def test_a_moment_is_a_time_element_a_table_can_sort_on(self):
        moment = this_year(10, 3, 9, 29, 15)

        self.assertEqual(self.moment(moment), (moment.isoformat(), when_exact(moment), "Oct\xa03, 9:29\xa0AM"))

    def test_an_age_keeps_the_exact_moment_behind_it(self):
        moment = timezone.now() - timedelta(days=5, hours=15)

        self.assertEqual(self.moment(moment, "ago"), (moment.isoformat(), when_exact(moment), "5\xa0days ago"))

    def test_a_date_is_a_date(self):
        self.assertEqual(self.moment(date(2019, 10, 3)), ("2019-10-03", "Oct\xa03,\xa02019", "Oct\xa03,\xa02019"))
        self.assertEqual(self.moment(date(2019, 10, 3), "ago")[2], "Oct\xa03,\xa02019")

    def test_the_day_and_exact_forms(self):
        moment = datetime(2019, 10, 3, 9, 29, 15, tzinfo=CHICAGO)

        self.assertEqual(self.moment(moment, "day")[2], "Oct\xa03,\xa02019")
        self.assertEqual(self.moment(moment, "exact")[2], "Oct\xa03,\xa02019, 9:29:15\xa0AM\xa0CDT")

    def test_the_calendar_forms_keep_the_day_behind_their_words(self):
        day = date(2026, 10, 5)

        self.assertEqual(self.moment(day, "weekday"), ("2026-10-05", "Oct\xa05,\xa02026", "Monday"))
        self.assertEqual(self.moment(day, "initials")[2], "Mo")
        self.assertEqual(self.moment(day, "month")[2], "October\xa02026")
        self.assertEqual(self.moment(day, "numeral")[2], "5")

    def test_the_length_and_ahead_forms_keep_the_exact_moment_behind_them(self):
        moment = timezone.now() + timedelta(days=3, hours=2)

        self.assertEqual(self.moment(moment, "length"), (moment.isoformat(), when_exact(moment), "3\xa0days"))
        self.assertEqual(self.moment(moment, "ahead")[2], "in 3\xa0days")

    def test_an_iso_stamp_reads_like_the_instant_it_names(self):
        moment = this_year(10, 3, 9, 29, 15)

        self.assertEqual(self.moment(moment.isoformat()), self.moment(moment))
        self.assertEqual(self.moment("2019-10-03")[0], "2019-10-03")

    def test_nothing_is_the_missing_mark_and_other_text_is_left_alone(self):
        self.assertEqual(text(self.render(None)), MISSING)
        self.assertEqual([mark.words for mark in with_class(self.render(""), "empty-value")], [MISSING])
        self.assertEqual(self.render("Tailnet"), "Tailnet")

    def test_an_unknown_form_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            self.render(timezone.now(), "short")


class OneOwnerTests(SimpleTestCase):
    """Django is handed the formats ``moments`` owns."""

    def test_django_is_handed_the_owners_formats(self):
        from hq.config.formats.en import formats as handed

        from .. import moments

        self.assertEqual(handed.DATE_FORMAT, moments.DAY_YEAR_FORMAT)
        self.assertEqual(handed.DATETIME_FORMAT, moments.MOMENT_YEAR_FORMAT)
        self.assertEqual(handed.TIME_FORMAT, moments.CLOCK_FORMAT)
        self.assertEqual(handed.YEAR_MONTH_FORMAT, moments.MONTH_FORMAT)


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
        from datetime import date, datetime

        from django.utils import timezone

        from ..projection import day_span

        self.assertIsNone(day_span(date(2026, 3, 1))[1])
        late = datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
        self.assertEqual(day_span(late)[0], day_span(timezone.localtime(late).date())[0])

    def test_it_matches_what_a_date_cast_would_have_kept(self):
        from datetime import date, datetime, timedelta

        from hq.platform.core.models import AuditLog

        from ..projection import day_span

        day = date(2026, 3, 1)
        for hours in range(-30, 54, 3):
            row = AuditLog.objects.create(action=AuditLog.Action.CREATED, object_type="Example")
            AuditLog.objects.filter(pk=row.pk).update(
                created_at=datetime(2026, 3, 1, 12, tzinfo=UTC) + timedelta(hours=hours)
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
        self.assertEqual(span(301), "9\xa0months, 3\xa0weeks")
        self.assertEqual(span(1443), "3\xa0years, 11\xa0months")
        self.assertEqual(span(-3), "3 days overdue")
