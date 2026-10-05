from django.shortcuts import render

from hq_sdk.outbound import ask
from hq_sdk.ui import Kpi

from .outbound import ARCHIVED, LOOKUP, NOTES, stored


def index(request):
    return render(
        request,
        "example_hq_plugin/index.html",
        {
            "example_metrics": (Kpi("Notes", len(NOTES), "Two notes and an archived one"),),
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
