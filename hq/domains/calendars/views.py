"""The calendar: a month of every source, a day of everything, and the operator's own events."""

from datetime import date
from typing import override
from uuid import UUID

from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import CreateView, DeleteView, RedirectView, TemplateView, UpdateView

from hq.platform.application.calendar import calendar_day, calendar_month, month_of, month_url
from hq.platform.application.calendar_entries import (
    CHOOSE_SOURCE,
    calendar_choices,
    repeat_label,
    upcoming,
    when_label,
)
from hq.platform.application.capabilities import capability_title
from hq.platform.application.card_items import ListRow
from hq.platform.application.entity_links import web_url
from hq.platform.application.moments import stamp_day, when_day
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.references import ReferencePickerMixin, reference_of
from hq.platform.application.security import Capability, safe_next, web_principal
from hq.platform.application.web_writes import execute_posted, refusal_response
from hq.platform.application.writes import CapabilityDeleteMixin, CapabilityFormMixin, CommandFormPage

from .forms import EntryForm
from .models import Entry

# What makes a new event, its form and its command.
CREATE = "calendar.entry.create"
UPDATE = "calendar.entry.update"
DELETE = "calendar.entry.delete"

CALENDAR_TRAIL = (("Calendar", reverse_lazy("calendar:month")),)


class CalendarView(PageMixin, TemplateView):
    # The calendar is personal, so reading it is a grant of its own, never READ.
    required_capability = Capability.READ_CALENDAR
    template_name = "calendars/calendar.html"
    page_title = "Calendar"

    def _selected(self) -> date | None:
        return stamp_day(self.request.GET.get("day", ""))

    @override
    def get_page_actions(self):
        day = self._selected() or timezone.localdate()
        return (PageAction.command(CREATE, primary=True, query={"on": day.isoformat()}),)

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
            upcoming=_upcoming(calendar),
            entry=self._entry(selected),
            previous_url=month_url(calendar.previous, selected),
            next_url=month_url(calendar.next, selected),
            today_url=month_url(today.replace(day=1), today),
            here=self.request.get_full_path(),
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
            "next": [(when, month_url(when.replace(day=1), when)) for when in upcoming(entry, count=4) if when != day][
                :3
            ]
            if entry.repeat
            else (),
            "actions": (
                PageAction.command("calendar.entry.update", entry.uid),
                PageAction.command("calendar.entry.delete", entry.uid),
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


def _upcoming(calendar) -> tuple:
    """What is ahead in the month shown, as the shared record rows: each
    title in full beside its day, which a narrow grid cannot spell out."""

    rows = []
    for placed in calendar.upcoming:
        event = placed.event
        day = max(event.first_day, calendar.today)
        rows.append(
            ListRow(
                title=event.title,
                meta=" ".join(filter(None, (when_day(day), event.time_label))),
                url=event.url or month_url(day.replace(day=1), day),
                external=bool(web_url(event.url)),
            )
        )
    return tuple(rows)


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
                external=bool(web_url(event.url)),
            )
            for event in events
            if not (opened and event.id.startswith(f"{opened}:"))
        )
        if rows:
            found.append((source, rows))
    return tuple(found)


class CalendarSourceView(View):
    """Check or uncheck one source, then go back to the month it was chosen on."""

    required_capability = Capability.READ_CALENDAR

    def post(self, request, source_id):
        result = execute_posted(
            request, CHOOSE_SOURCE, {"source": source_id, "shown": request.POST.get("shown") == "1"}
        )
        if not result["ok"]:
            return refusal_response(result)
        return HttpResponseRedirect(safe_next(request, fallback=reverse("calendar:month"), scope="/calendar/"))


class EntryPage(PageMixin):
    required_capability = Capability.READ_CALENDAR
    model = Entry
    slug_field = "uid"
    slug_url_kwarg = "uid"

    page_trail = CALENDAR_TRAIL


class EntryWrite:
    """How an event's write names it; each write names its own capabilities,
    so no base holds a placeholder for them that another base contradicts."""

    result_key = "entry"
    identity_attr = "uid"


# The fields its form page draws beyond the form's own loop.
FORM_PAGE = {"form_fields": "calendars/_entry_fields.html"}


class EntryFormPage(EntryWrite, CommandFormPage, EntryPage, ReferencePickerMixin, CapabilityFormMixin):
    """An event's form, said as its command's page says it."""

    creates = CREATE
    updates = UPDATE
    listed_route = "calendar:month"
    extra_context = FORM_PAGE
    form_class = EntryForm


class EntryDetailView(RedirectView):
    """An entry's own address opens it on the calendar."""

    required_capability = Capability.READ_CALENDAR

    @override
    def get_redirect_url(self, uid):
        return get_object_or_404(Entry, uid=uid).get_absolute_url()


class EntryCreateView(EntryFormPage, CreateView):
    @override
    def get_page_title(self):
        return capability_title(CREATE)

    @override
    def get_initial(self):
        on = stamp_day(self.request.GET.get("on", ""))
        return {"starts_on": on or timezone.localdate()}


class EntryUpdateView(EntryFormPage, UpdateView):
    page_title = "Edit event"


class EntryDeleteView(EntryWrite, EntryPage, CapabilityDeleteMixin, DeleteView):
    deletes = DELETE
    page_title = "Delete event?"
    template_name = "calendars/entry_confirm_delete.html"
    success_url = reverse_lazy("calendar:month")
    context_object_name = "entry"
