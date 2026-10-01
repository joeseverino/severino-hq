"""The one calendar: its contract, its month, My Calendar and where it is listed."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from calendars.models import Entry
from core.models import AuditLog

from ..calendar import (
    CalendarEvent,
    CalendarSource,
    calendar_day,
    calendar_month,
    calendar_sources,
    month_of,
)
from ..calendar_entries import (
    EntryCommand,
    NotFoundError,
    choose_source,
    calendar_choices,
    list_agenda,
    occurrences,
    save_entry,
    upcoming,
)
from ..deletion import DeleteCommand, delete_calendar_entry
from ..domains import Domain, domain_navigation, host_domains
from ..plugins import NavigationItem, PluginIntegration
from ..security import AuthorizationError, Capability, Principal, cli_principal, mcp_principal

TODAY = date(2026, 10, 14)


def _domain(sources, *, id="example.domain", label="Example", navigation=()):
    return Domain(
        id=id,
        label=label,
        origin="extension",
        navigation=navigation,
        integration=PluginIntegration(calendars=lambda: sources),
    )


def _with(*domains):
    """The host's domains plus these, as the calendar composes them."""

    return mock.patch("application.domains.all_domains", return_value=(*host_domains(), *domains))


def _source(id, events, **kwargs):
    return CalendarSource(id=id, label=id.split(".")[-1].title(), events=lambda first, last: events, **kwargs)


class ContractTests(SimpleTestCase):
    def test_an_event_refuses_what_cannot_be_drawn(self):
        aware = timezone.make_aware(datetime(2026, 10, 1, 9))
        for kwargs in (
            {"id": "", "title": "x", "starts": TODAY},
            {"id": "a", "title": " ", "starts": TODAY},
            {"id": "a", "title": "x", "starts": TODAY, "state": "maybe"},
            {"id": "a", "title": "x", "starts": datetime(2026, 10, 1, 9)},
            {"id": "a", "title": "x", "starts": TODAY, "ends": TODAY - timedelta(days=1)},
            {"id": "a", "title": "x", "starts": TODAY, "ends": aware},
            {"id": "a", "title": "x", "starts": TODAY, "ends": TODAY + timedelta(days=3), "mark": True},
        ):
            with self.subTest(**{k: str(v) for k, v in kwargs.items()}), self.assertRaises(ValueError):
                CalendarEvent(**kwargs)

    def test_an_end_is_exclusive_as_in_icalendar(self):
        span = CalendarEvent("a", "Trip", TODAY, TODAY + timedelta(days=3))
        self.assertEqual((span.first_day, span.last_day, span.span), (TODAY, TODAY + timedelta(days=2), True))
        zone = timezone.get_current_timezone()
        late = CalendarEvent(
            "b", "Late", datetime.combine(TODAY, time(22), zone), datetime.combine(TODAY + timedelta(days=1), time(0), zone)
        )
        self.assertFalse(late.span)
        self.assertEqual(late.time_label, "10p")

    def test_a_link_is_hqs_own_path_or_a_web_address(self):
        self.assertEqual(CalendarEvent("a", "x", TODAY, url="/calendar/").url, "/calendar/")
        self.assertEqual(CalendarEvent("a", "x", TODAY, url="javascript:alert(1)").url, "")

    def test_a_source_names_whose_it_is(self):
        for bad in ("calendar", "Example.Sessions", "example.", "example sessions"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                CalendarSource(id=bad, label="x", events=lambda first, last: ())
        with self.assertRaises(ValueError):
            CalendarSource(id="a.b", label="x", events=lambda first, last: (), slot=11)

    def test_a_month_parameter_falls_back_to_this_month(self):
        self.assertEqual(month_of("2026-02", TODAY), date(2026, 2, 1))
        for bad in ("", "2026", "2026-13", "x-y", None):
            with self.subTest(bad=bad):
                self.assertEqual(month_of(bad, TODAY), date(2026, 10, 1))


class MonthTests(TestCase):
    def test_six_sunday_first_weeks_with_today_and_the_past(self):
        with _with():
            month = calendar_month(date(2026, 10, 1), today=TODAY)
        self.assertEqual(len(month.weeks), 6)
        self.assertEqual(month.weeks[0][0].day, date(2026, 9, 27))
        cells = [cell for week in month.weeks for cell in week]
        self.assertEqual([cell.day for cell in cells if cell.today], [TODAY])
        self.assertTrue(next(cell for cell in cells if cell.day == date(2026, 10, 13)).past)
        self.assertFalse(next(cell for cell in cells if cell.day == date(2026, 9, 30)).in_month)
        self.assertEqual((month.previous, month.next), (date(2026, 9, 1), date(2026, 11, 1)))

    def test_a_span_keeps_its_lane_across_the_week(self):
        trip = CalendarEvent("trip", "Trip", date(2026, 10, 12), date(2026, 10, 16))
        short = CalendarEvent("short", "Short", date(2026, 10, 14), date(2026, 10, 16))
        with _with(_domain([_source("example.spans", [trip, short])])):
            month = calendar_month(date(2026, 10, 1), today=TODAY)
        week = next(week for week in month.weeks if week[0].day == date(2026, 10, 11))
        monday, tuesday, wednesday = week[1], week[2], week[3]
        self.assertEqual(monday.lanes[0].placed.event.id, "trip")
        self.assertTrue(monday.lanes[0].starts_here and monday.lanes[0].titled)
        self.assertFalse(tuesday.lanes[0].titled)
        self.assertEqual(wednesday.lanes[0].placed.event.id, "trip")
        self.assertEqual(wednesday.lanes[1].placed.event.id, "short")
        self.assertIsNone(tuesday.lanes[1])
        # A span carried into a new week is titled again on its first day there.
        later = CalendarEvent("long", "Long", date(2026, 10, 16), date(2026, 10, 20))
        with _with(_domain([_source("example.spans", [later])])):
            month = calendar_month(date(2026, 10, 1), today=TODAY)
        sunday = next(cell for week in month.weeks for cell in week if cell.day == date(2026, 10, 18))
        self.assertTrue(sunday.lanes[0].titled)

    def test_a_busy_day_folds_and_marks_stay_dots(self):
        items = [CalendarEvent(f"i{n}", f"Item {n}", TODAY) for n in range(5)]
        marks = [CalendarEvent(f"m{n}", "Run", TODAY, mark=True, state="done") for n in range(3)]
        with _with(_domain([_source("example.busy", [*items, *marks])])):
            cell = next(
                cell
                for week in calendar_month(date(2026, 10, 1), today=TODAY).weeks
                for cell in week
                if cell.day == TODAY
            )
        self.assertEqual((len(cell.items), cell.more, len(cell.marks)), (3, 2, 3))

    def test_an_unchecked_source_leaves_the_grid_but_not_the_day(self):
        event = CalendarEvent("x", "Quiet", TODAY)
        with _with(_domain([_source("example.quiet", [event], shown=False)])):
            month = calendar_month(date(2026, 10, 1), today=TODAY)
            day = calendar_day(TODAY, today=TODAY)
            checked = calendar_month(date(2026, 10, 1), choices={"example.quiet": True}, today=TODAY)
        drawn = lambda month: [i for w in month.weeks for c in w for i in c.items]  # noqa: E731
        self.assertEqual(drawn(month), [])
        self.assertEqual([event.title for _source, events in day.groups for event in events], ["Quiet"])
        self.assertEqual(len(drawn(checked)), 1)

    def test_a_failing_source_is_said_beside_its_name_and_the_rest_still_draw(self):
        def broken(first, last):
            raise RuntimeError("down")

        fine = CalendarEvent("ok", "Fine", TODAY)
        with _with(
            _domain([CalendarSource("example.broken", "Broken", broken), _source("example.fine", [fine])])
        ):
            month = calendar_month(date(2026, 10, 1), today=TODAY)
        views = {view.id: view for _group, views in month.groups for view in views}
        self.assertEqual(views["example.broken"].failure, "Could not be read just now")
        self.assertEqual(views["example.fine"].count, 1)

    def test_what_shows_by_default_is_told_apart_and_history_takes_the_rest(self):
        shown = [_source(f"example.s{n}", []) for n in range(5)]
        hidden = [_source(f"example.h{n}", [], shown=False) for n in range(3)]
        with _with(_domain([_source("example.asks", [], slot=1), *hidden, *shown])):
            groups = calendar_month(date(2026, 10, 1), today=TODAY).groups
        views = [view for _group, views in groups for view in views]
        defaults = [view.slot for view in views if view.shown]
        self.assertEqual(len(set(defaults)), len(defaults))
        self.assertEqual(next(view.slot for view in views if view.id == "example.asks"), 1)

    def test_two_domains_cannot_claim_one_source(self):
        with _with(_domain([_source("example.same", [])]), _domain([_source("example.same", [])], id="other")):
            with self.assertRaises(ValueError):
                calendar_sources()

    def test_a_source_keeps_the_colour_it_asks_for_and_the_rest_are_dealt(self):
        with _with(_domain([_source("example.a", [], slot=3), _source("example.b", []), _source("example.c", [])])):
            groups = calendar_month(date(2026, 10, 1), today=TODAY).groups
        slots = {view.id: view.slot for _group, views in groups for view in views}
        self.assertEqual(slots["example.a"], 3)
        self.assertEqual(len({slots["example.b"], slots["example.c"], 3}), 3)


def _entry(**fields):
    defaults = {"title": "Entry", "starts_on": date(2026, 10, 1)}
    entry = Entry(**{**defaults, **fields})
    entry.full_clean()
    entry.save()
    return entry


class RepeatTests(TestCase):
    def _days(self, entry, first=date(2026, 10, 1), last=date(2026, 10, 31)):
        return list(occurrences(entry, first, last))

    def test_once(self):
        self.assertEqual(self._days(_entry()), [date(2026, 10, 1)])
        self.assertEqual(self._days(_entry(), date(2026, 11, 1), date(2026, 11, 30)), [])

    def test_daily_every_other_day_until(self):
        entry = _entry(repeat="daily", interval=2, repeat_until=date(2026, 10, 7))
        self.assertEqual(self._days(entry), [date(2026, 10, d) for d in (1, 3, 5, 7)])

    def test_weekly_on_named_days(self):
        entry = _entry(repeat="weekly", weekdays="0,2,4", starts_on=date(2026, 10, 1))
        days = self._days(entry, date(2026, 10, 1), date(2026, 10, 11))
        self.assertEqual(days, [date(2026, 10, d) for d in (2, 5, 7, 9)])

    def test_monthly_on_the_31st_skips_short_months(self):
        entry = _entry(repeat="monthly", starts_on=date(2026, 1, 31))
        self.assertEqual(
            self._days(entry, date(2026, 1, 1), date(2026, 5, 31)),
            [date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31)],
        )

    def test_yearly_on_the_29th_of_february_waits_for_a_leap_year(self):
        entry = _entry(repeat="yearly", starts_on=date(2024, 2, 29))
        self.assertEqual(self._days(entry, date(2025, 1, 1), date(2028, 12, 31)), [date(2028, 2, 29)])

    def test_a_multi_day_occurrence_reaching_into_the_window_counts(self):
        entry = _entry(starts_on=date(2026, 9, 29), ends_on=date(2026, 10, 2))
        self.assertEqual(self._days(entry), [date(2026, 9, 29)])

    def test_upcoming_starts_from_today(self):
        entry = _entry(repeat="weekly", starts_on=date(2026, 10, 1))
        self.assertEqual(upcoming(entry, count=2, today=TODAY), (date(2026, 10, 15), date(2026, 10, 22)))

    def test_what_an_entry_cannot_say(self):
        for fields in (
            {"title": " "},
            {"ends_on": date(2026, 9, 1)},
            {"ends_at": time(10)},
            {"starts_at": time(10), "ends_at": time(9)},
            {"weekdays": "1"},
            {"repeat": "weekly", "weekdays": "7"},
            {"repeat_until": date(2026, 12, 1)},
            {"repeat": "daily", "interval": 0},
        ):
            with self.subTest(fields=fields), self.assertRaises(Exception):
                _entry(**fields)


class WriteTests(TestCase):
    def test_an_entry_is_audited_and_attributed(self):
        result = save_entry(
            EntryCommand(title="Dentist", starts_on=TODAY, starts_at=time(15)), principal=cli_principal()
        )
        self.assertTrue(result["created"])
        self.assertTrue(
            AuditLog.objects.filter(object_type="Calendar entry", action=AuditLog.Action.CREATED).exists()
        )
        agenda = list_agenda(start=TODAY, days=1)
        self.assertIn("Dentist", [item["title"] for item in agenda["items"]])

    def test_writing_and_removing_need_their_capabilities(self):
        reader = Principal("reader", "mcp", frozenset({Capability.READ}))
        with self.assertRaises(AuthorizationError):
            save_entry(EntryCommand(title="x", starts_on=TODAY), principal=reader)
        entry = _entry()
        with self.assertRaises(AuthorizationError):
            delete_calendar_entry(DeleteCommand(confirm=str(entry.uid)), principal=reader, current_key=str(entry.uid))
        with self.assertRaises(ValueError):
            delete_calendar_entry(DeleteCommand(confirm="other"), principal=cli_principal(), current_key=str(entry.uid))
        delete_calendar_entry(DeleteCommand(confirm=str(entry.uid)), principal=cli_principal(), current_key=str(entry.uid))
        self.assertFalse(Entry.objects.exists())

    def test_an_agent_has_the_calendar_only_on_its_own_switch(self):
        with override_settings(SEVERINO_MCP_ENABLE_CALENDAR=False, SEVERINO_MCP_ENABLE_WRITES=True):
            self.assertFalse(mcp_principal().permits(Capability.READ_CALENDAR))
            self.assertFalse(mcp_principal().permits(Capability.WRITE_CALENDAR))
        with override_settings(SEVERINO_MCP_ENABLE_CALENDAR=True, SEVERINO_MCP_ENABLE_WRITES=False):
            agent = mcp_principal()
            self.assertTrue(agent.permits(Capability.WRITE_CALENDAR))
            self.assertFalse(agent.permits(Capability.WRITE_EXPENSES))
            self.assertFalse(agent.permits(Capability.DELETE_CALENDAR))

    def test_choices_are_per_operator_and_name_a_real_source(self):
        user = get_user_model().objects.create_user("calendar-chooser")
        choose_source(user, "history.deploys", True)
        self.assertEqual(calendar_choices(user), {"history.deploys": True})
        with self.assertRaises(NotFoundError):
            choose_source(user, "no.such", False)


class PlacementTests(SimpleTestCase):
    def test_the_calendar_sits_beside_the_dashboard_until_an_extension_places_it(self):
        with _with():
            routes = [(item.route, item.group) for item in domain_navigation()]
        self.assertIn(("calendar:month", ""), routes)

        placed = _domain([], navigation=(NavigationItem("Calendar", "calendar:month", "calendar", 10, "Example"),))
        with _with(placed):
            calendar = [item for item in domain_navigation() if item.route == "calendar:month"]
        self.assertEqual([(item.group, item.order) for item in calendar], [("Example", 10)])


class PageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("calendar-page"))

    def test_the_month_the_day_and_an_entry_link_together(self):
        entry = _entry(title="Dentist", starts_on=timezone.localdate())
        response = self.client.get(reverse("calendar:month"), {"day": timezone.localdate().isoformat()})
        self.assertContains(response, "Dentist")
        self.assertContains(response, entry.get_absolute_url().replace("&", "&amp;"))
        self.assertContains(response, 'data-hotkey="t"')
        self.assertContains(response, "Add to My Calendar")
        # An entry opens beside its day: once, with what can be done to it.
        opened = self.client.get(entry.get_absolute_url())
        self.assertContains(opened, '<article class="day-panel-entry"')
        self.assertContains(opened, reverse("calendar:entry_edit", args=[entry.uid]))
        panel = opened.content.decode().split('<section class="card day-panel"', 1)[1].split("</section>", 1)[0]
        listed = panel.split("</article>", 1)[1]
        self.assertNotIn("Dentist", listed)
        # Its old address opens the same place.
        self.assertRedirects(
            self.client.get(reverse("calendar:entry", args=[entry.uid])),
            entry.get_absolute_url(),
            fetch_redirect_response=False,
        )

    def test_a_bar_keeps_its_height_across_the_week(self):
        """Marks share the date's row, so a dot in one cell and not the next
        cannot step a bar down where the dot is."""

        _entry(title="Trip", starts_on=timezone.localdate(), ends_on=timezone.localdate() + timedelta(days=2))
        mark = CalendarEvent("run", "Run", timezone.localdate(), mark=True, state="done")
        with _with(_domain([_source("example.marks", [mark])])):
            body = self.client.get(reverse("calendar:month")).content.decode()
        cell = body[body.rindex("<td", 0, body.index('class="month-mark')):]
        cell = cell[: cell.index("</td>")]
        self.assertLess(cell.index("month-day-head"), cell.index("month-mark"))
        self.assertLess(cell.index("month-mark"), cell.index("month-span"))

    def test_checking_a_source_returns_to_the_month_it_was_chosen_on(self):
        here = f"{reverse('calendar:month')}?month=2026-10"
        response = self.client.post(
            reverse("calendar:source", args=["history.deploys"]), {"shown": "1", "next": here}
        )
        self.assertRedirects(response, here, fetch_redirect_response=False)
        response = self.client.post(
            reverse("calendar:source", args=["history.deploys"]), {"shown": "1", "next": "https://evil.example/"}
        )
        self.assertRedirects(response, reverse("calendar:month"), fetch_redirect_response=False)
        self.assertEqual(
            self.client.post(reverse("calendar:source", args=["no.such"]), {"shown": "1"}).status_code, 404
        )

    def test_the_form_writes_through_the_command(self):
        response = self.client.post(
            reverse("calendar:entry_new"),
            {"title": "Oil change", "starts_on": "2026-10-17", "repeat": "", "interval": 1},
        )
        entry = Entry.objects.get(title="Oil change")
        self.assertRedirects(response, entry.get_absolute_url(), fetch_redirect_response=False)
        response = self.client.post(
            reverse("calendar:entry_new"),
            {"title": "Bad", "starts_on": "2026-10-17", "weekdays": ["1"], "repeat": "", "interval": 1},
        )
        self.assertContains(response, "Only a weekly entry names its days.")

    def test_a_day_of_history_opens_on_its_own(self):
        response = self.client.get(reverse("core:audit_list"), {"on": "2026-10-14"})
        self.assertEqual(response.status_code, 200)
