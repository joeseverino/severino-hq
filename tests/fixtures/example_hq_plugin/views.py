from django.shortcuts import render

from hq_sdk.outbound import ask
from hq_sdk.pages import page_context
from hq_sdk.ui import Counted, Kpi
from hq_sdk.web import capability_required

from .outbound import ARCHIVED, LOOKUP, NOTES, stored


@capability_required("notes.read")
def index(request):
    return render(
        request,
        "example_hq_plugin/index.html",
        {
            **page_context("Example Notes", "A minimal plugin composed entirely from the HQ UI contract."),
            "example_metrics": (Kpi(Counted(len(NOTES), "Note"), detail="Two notes and an archived one"),),
            # What is stored, and the control that asks for it to be read
            # again. The view reaches nothing outside the process.
            "notes": tuple(
                {
                    "slug": slug,
                    "entries": stored(slug),
                    "lookup": ask(LOOKUP, slug, refresh="#example-notes", compact=True),
                }
                for slug in (*NOTES, ARCHIVED)
            ),
        },
    )
