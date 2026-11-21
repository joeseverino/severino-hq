from django.shortcuts import render

from hq_sdk.outbound import ask
from hq_sdk.pages import page_context
from hq_sdk.tables import Cell, DataTable, Row, TableColumn
from hq_sdk.ui import Counted, Kpi
from hq_sdk.web import capability_required

from .outbound import ARCHIVED, LOOKUP, NOTES, stored


def _note(slug):
    """A note, what the registry lists for it, and the control that asks for it to be read again."""

    entries = stored(slug)
    listed = Cell(parts=tuple(map(Cell, entries)), fit=True) if entries else Cell("Not looked up", muted=True)
    lookup = ask(LOOKUP, slug, refresh="#example-notes", compact=True)
    return Row((Cell(slug, readout=True), listed), asks=(lookup,))


@capability_required("notes.read")
def index(request):
    # What is stored, and the control that asks for it to be read again. The
    # view reaches nothing outside the process.
    notes = tuple(_note(slug) for slug in (*NOTES, ARCHIVED))
    return render(
        request,
        "example_hq_plugin/index.html",
        {
            **page_context("Example Notes", "A minimal plugin composed entirely from the HQ UI contract."),
            "example_metrics": (Kpi(Counted(len(NOTES), "Note"), detail="Two notes and an archived one"),),
            "notes": DataTable(
                "Notes", (TableColumn("Note", kind="identifier"), TableColumn("The registry lists")), notes
            ),
        },
    )
