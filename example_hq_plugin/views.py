from django.shortcuts import render

from application.ui import Kpi


def index(request):
    return render(
        request,
        "example_hq_plugin/index.html",
        {
            "example_metrics": (Kpi("Notes", 0, "No records yet", is_zero=True),)
        },
    )
