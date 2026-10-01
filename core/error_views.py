"""The page HQ shows when it fails, built from nothing that may have failed."""

from django.http import HttpResponseServerError
from django.template import loader

from .context_processors import site


def server_error(request):
    """Django's 500 skips the context processors, rightly: any of them may be
    what broke. HQ's name and asset version come from settings alone, so this
    one is given to the page and the rest are not."""

    return HttpResponseServerError(loader.get_template("500.html").render(site(request)))
