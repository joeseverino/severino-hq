"""The calendar: a month of every source, a day of everything, and the operator's own events."""

from datetime import date
from typing import override
from uuid import UUID

from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, RedirectView, TemplateView, UpdateView

from hq.platform.application.calendar import calendar_day, calendar_month, month_of
from hq.platform.application.calendar_entries import (
    NotFoundError,
    calendar_choices,
    choose_source,
    entry_command,
    repeat_label,
    save_entry,
    upcoming,
    when_label,
)
from hq.platform.application.deletion import delete_calendar_entry
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.references import ReferencePickerMixin, reference_of
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.ui import ListRow
from hq.platform.application.writes import ServiceCreateMixin, ServiceDeleteMixin, ServiceUpdateMixin

from .forms import EntryForm
from .models import Entry

CALENDAR_TRAIL = ("Calendar", reverse_lazy("calendar:month"))


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def month_url(month: date, day: date | None = None) -> str:
    query = f"?month={month:%Y-%m}"
    return f"{reverse('calendar:month')}{query}{f'&day={day.isoformat()}' if day else ''}"


class CalendarView(PageMixin, TemplateView):
    template_name = "calendars/calendar.html"
    page_title = "Calendar"

    def _selected(self) -> date | None:
        return _day(self.request.GET.get("day", ""))

    @override
    def get_page_actions(self):
        day = self._selected() or timezone.localdate()
        return (
            PageAction(
                "Add event", f"{reverse('calendar:entry_new')}?on={day.isoformat()}", primary=True
            ),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        selected = self._selected()
        month = month_of(self.request.GET.get("month"), today)
        if selected and "month" not in self.request.GET:
            month = selected.replace(day=1)
        choices = calendar_choices(self.request.user)
        calendar = calendar_month(month, choices=choices, today=today)
        day = calendar_day(selected, choices=choices, today=today) if selected else None
        context.update(
            calendar=calendar,
            selected=selected,
            day=day,
            day_groups=_rows(day, opened=self.request.GET.get("entry", "")) if day else (),
            entry=self._entry(selected),
            previous_url=month_url(calendar.previous, selected),
            next_url=month_url(calendar.next, selected),
            today_url=month_url(today.replace(day=1), today),
            here=self.request.get_full_path(),
            new_entry_url=reverse("calendar:entry_new"),
        )
        return context


    def _entry(self, day: date | None) -> dict | None:
        """The entry opened beside the month: what it is, and what can be done to it."""

        uid = self.request.GET.get("entry", "")
        entry = Entry.objects.filter(uid=uid).first() if day and _is_uuid(uid) else None
        if entry is None:
            return None
        return {
            "object": entry,
            "about": reference_of(entry, "about", principal=web_principal(self.request.user)),
            "when": when_label(entry),
            "repeat": repeat_label(entry),
            "next": [
                (when, month_url(when.replace(day=1), when))
                for when in upcoming(entry, count=4)
                if when != day
            ][:3]
            if entry.repeat
            else (),
            "actions": (
                PageAction("Edit", reverse("calendar:entry_edit", args=[entry.uid])),
                PageAction("Delete", reverse("calendar:entry_delete", args=[entry.uid]), danger=True),
            ),
        }


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _range(event) -> str:
    """A span's days, as the day panel names them beside its title."""

    if not event.span:
        return ""
    first, last = event.first_day, event.last_day
    end = f"{last:%b} {last.day}" if last.month != first.month else str(last.day)
    return f"{first:%b} {first.day} – {end}"


def _rows(day, *, opened: str = "") -> tuple:
    """The day's events as the shared record rows, by source; the one opened
    above them is not listed again."""

    found = []
    for source, events in day.groups:
        rows = tuple(
            ListRow(
                title=event.title,
                detail=event.detail,
                meta=event.time_label or _range(event),
                url=event.url,
                external=event.url.startswith("http"),
            )
            for event in events
            if not (opened and event.id.startswith(f"{opened}:"))
        )
        if rows:
            found.append((source, rows))
    return tuple(found)


class CalendarSourceView(View):
    """Check or uncheck one source, then go back to the month it was chosen on."""

    def post(self, request, source_id):
        try:
            choose_source(request.user, source_id, request.POST.get("shown") == "1")
        except NotFoundError as exc:
            raise Http404(str(exc)) from exc
        return HttpResponseRedirect(safe_next(request, fallback=reverse("calendar:month"), scope="/calendar/"))


class EntryPage(PageMixin):
    model = Entry
    slug_field = "uid"
    slug_url_kwarg = "uid"

    @override
    def get_page_trail(self):
        return (CALENDAR_TRAIL,)


class EntryWrite:
    model = Entry
    noun = "Event"
    result_key = "entry"
    identity_attr = "uid"
    identity_kwarg = "current_key"


class EntryDetailView(RedirectView):
    """An entry's own address opens it on the calendar."""

    @override
    def get_redirect_url(self, uid):
        return get_object_or_404(Entry, uid=uid).get_absolute_url()


class EntryCreateView(EntryWrite, EntryPage, ReferencePickerMixin, ServiceCreateMixin, CreateView):
    page_title = "Add event"
    form_class = EntryForm
    template_name = "calendars/entry_form.html"
    service = staticmethod(save_entry)
    command_from_cleaned_data = staticmethod(entry_command)

    @override
    def get_initial(self):
        on = _day(self.request.GET.get("on", ""))
        return {"starts_on": on or timezone.localdate()}


class EntryUpdateView(EntryWrite, EntryPage, ReferencePickerMixin, ServiceUpdateMixin, UpdateView):
    page_title = "Edit event"
    form_class = EntryForm
    template_name = "calendars/entry_form.html"
    service = staticmethod(save_entry)
    command_from_cleaned_data = staticmethod(entry_command)


class EntryDeleteView(EntryWrite, EntryPage, ServiceDeleteMixin, DeleteView):
    page_title = "Delete event?"
    template_name = "calendars/entry_confirm_delete.html"
    success_url = reverse_lazy("calendar:month")
    context_object_name = "entry"
    service = staticmethod(delete_calendar_entry)
