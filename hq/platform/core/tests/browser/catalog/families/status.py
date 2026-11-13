"""The catalog's statuses: a status in each form it takes."""

from hq.platform.core.tests.browser.catalog.common import Entry

ENTRIES = {
    # A status as its words in its tone, among figures, as a change is drawn.
    "status/toned": Entry(
        '{% section "Example changes" %}<p>{% status "+$120.00" tone="good" pill=False %}'
        ' {% status "−$40.00" tone="attention" pill=False %} {% status "−$900.00" tone="serious" pill=False %}'
        ' {% status "$0.00" tone="neutral" pill=False %}</p>{% endsection %}'
    ),
    # A standing state after its dot: its words in its tone, then a heading's
    # own words in the ink around them, the dot alone toned.
    "status/dot": Entry(
        '{% section "Example states" %}<p>{% status "Allowed" tone="good" dot=True %}'
        ' {% status "Paused" tone="attention" dot=True %} {% status "Refused" tone="serious" dot=True %}'
        ' {% status "Not tested" tone="neutral" dot=True %}</p>'
        '<p><strong>{% status "Example" tone="good" dot=True ink="context" %}</strong>'
        ' {% status "Examples" tone="attention" dot=True ink="context" %}</p>{% endsection %}'
    ),
    # The names a record carries, as chips beside its name: each as written.
    "status/chip": Entry(
        '{% section "Example project" %}<p><strong>example-app</strong> {% status "django" chip=True %}'
        '{% status "example.com" chip=True %}{% status "Reverse proxy" chip=True title="An example role" %}</p>'
        "{% endsection %}"
    ),
}
