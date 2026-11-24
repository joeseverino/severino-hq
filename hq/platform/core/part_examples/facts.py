"""The facts part's examples: a band, a list, and the figures a band leads with."""

lazy from datetime import date

from hq.platform.core import held_values
from hq.platform.core.part_examples import runs
from hq.platform.core.template_parts import Example
lazy from hq.platform.application.card_items import Counted, Kpi
lazy from hq.platform.application.drawings import AllocationBar, Dots, Share, Trend
lazy from hq.platform.application.entity_links import EntityLink
lazy from hq.platform.application.pages import Page, PageVisit
lazy from hq.platform.application.plugins import checked_cards
lazy from hq.platform.application.tone import Tone


class _Drawn:
    """A template's source, which a page head's ``page_aside`` draws as it would a template's name."""

    def __init__(self, source):
        self.source = source

    def render(self, context):
        return held_values.template(self.source).render(context)


# A page head's band of facts, as the dashboard's glance draws it, in each
# form a fact there takes: one leading to a dialog with a note of a problem,
# one whose line needs attention, and a region holding facts that open a
# panel, one needing attention, its note a run longer than a phone's line,
# and one past its time.
_GLANCE = (
    '<div data-example-region>{% facts layout="band" label="Example glance" %}'
    '{% fact "Example connection" icon="network" url="/example/" modal="example" %}'
    '{% status "Example" tone="good" dot=True ink="context" %}'
    '{% detail %}<small>Example TLS 1.3</small><small>{% status "An example check failed" tone="attention" pill=False %}</small>'
    "{% endfact %}"
    '{% fact "Example controller" icon="activity" url="/example/controller/" %}'
    '{% status "Silent" tone="attention" dot=True ink="context" %}'
    "{% detail %}<small>Last read an example day ago</small>{% endfact %}"
    '{% fact "example-host-01" icon="server" id="example-host" tone="attention" title="Example containers" '
    'data_fragment_part="example-host" %}12 <small>containers</small>'
    '{% detail %}<small>{% status "2 example alerts" tone="attention" pill=False %}</small>'
    '{% list kind="dots" %}<li>CPU 4%</li><li>Memory 48%</li><li>Storage 38.2 GB</li>{% endlist %}'
    "{% heading %}<strong>Example host</strong><span>An example summary</span>"
    '{% panel %}{% facts %}{% fact "Example CPU" value="4%" %}{% fact "Example memory" value="583 MB" %}{% endfacts %}'
    '{% foot %}<a href="/example/">Example containers</a><span><button type="button">Refresh</button>'
    '<a href="/example/settings/">Settings</a></span>{% endfact %}'
    '{% fact "Example weather" icon="cloud" id="example-weather" data_fragment_part="example-weather" %}71°'
    '{% detail %}{% list kind="dots" %}<li>Example Mostly Cloudy</li><li>71–86°</li>{% endlist %}'
    "<small>Last read an example 3 days ago · out of date</small>"
    "{% heading %}<strong>Example weather</strong>"
    "{% panel %}<p>The last reading failed (ExampleError). This is the one before it.</p>"
    '{% facts layout="band" size="small" items=hours label="Example hours ahead" %}'
    '{% foot %}<span><a href="/example/settings/">Settings</a></span>{% endfact %}'
    "{% endfacts %}</div>"
)


# The same band with nothing read yet, a long line that wraps inside its fact.
# Two readings beside a page's title, as a new install's dashboard draws them.
_GLANCE_TWO = (
    '<div data-example-region>{% facts layout="band" label="Example glance" %}'
    '{% fact "Example connection" icon="network" url="/example/" %}{% status "Example" tone="good" dot=True ink="context" %}'
    "{% detail %}<small>Example TLS 1.3</small>{% endfact %}"
    '{% fact "Example controller" icon="activity" url="/example/controller/" %}'
    '{% status "Online" tone="good" dot=True ink="context" %}{% detail %}<small>Last read just now</small>{% endfact %}'
    "{% endfacts %}</div>"
)


_GLANCE_EMPTY = (
    '<div data-example-region>{% facts layout="band" label="Example glance" %}'
    '{% fact "Example controller" icon="activity" url="/example/controller/" %}'
    '{% status "not set up"|absent dot=True ink="context" %}{% endfact %}'
    '{% fact "Example host" icon="server" id="example-empty" data_fragment_part="example-empty" %}{% empty_value %}'
    '{% detail %}<small>{% status "no reading yet"|absent pill=False %}</small>'
    "{% heading %}<strong>Example host</strong>{% panel %}<p>An example host has not been read yet.</p>{% endfact %}"
    '{% fact "Example weather" icon="cloud" id="example-long" data_fragment_part="example-long" %}71°'
    '{% detail %}{% list kind="dots" %}<li>Example Mostly Cloudy with an example chance of showers later</li>'
    "<li>71–86°</li><li>Example wind 12 mph</li>{% endlist %}"
    "{% heading %}<strong>Example weather</strong>{% panel %}<p>An example forecast.</p>{% endfact %}"
    "{% endfacts %}</div>"
)


# A forecast's hours, a strip of small figures: one with a note and one
# without, side by side, more than one line holds in a narrow box.
def _hours():
    return tuple(
        Kpi(f"{hour} PM", f"{80 - hour}°", "40%" if hour % 3 == 0 else "", tip="Example showers")
        for hour in range(1, 13)
    )


# Each value that fits on one line of its own.
_ONE_LINE = """(values) => [...document.querySelectorAll(values)]
  .filter((value) => Math.round(value.getBoundingClientRect().height / parseFloat(getComputedStyle(value).lineHeight)) < 2)
  .map((value) => value.textContent.trim())"""


# A list opened to read more set no closer than the page's list before it.
_TIGHTER = """(lists) => {
  const [page, opened] = [...document.querySelectorAll(lists)].map((list) => parseFloat(getComputedStyle(list).rowGap));
  return opened < page ? [] : [`the opened list's rows are ${opened}px apart, the page's ${page}px`];
}"""


# A press lands on each target at its middle, and on a lone value link at its label, past an open panel's cover.
_WHOLE = """(facts) => [...document.querySelectorAll(facts)].flatMap((fact) => {
  const label = fact.querySelector('.fact-label'), targets = [...fact.querySelectorAll('a[href], button')].filter((t) => t.checkVisibility());
  const lone = targets.length === 1 && targets[0].matches('.fact-value a') ? [[targets[0], label]] : [];
  const hit = (at) => { at.scrollIntoView({block: 'nearest'}); const box = at.getBoundingClientRect(); return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2); };
  return [...targets.map((t) => [t, t]), ...lone].filter(([target, at]) => { const press = hit(at), panel = press?.closest(':popover-open'); return !target.contains(press) && (!panel || panel.contains(target)); })
    .map(([target, at]) => `${label.textContent.trim()}: a press on ${at === label ? 'its label' : target.textContent.trim()} misses it`);
})"""


FACTS_STATES = {
    # A detail line naming something longer than its cell, cut at its end on
    # one line and read whole from its tip, over a line opening a tip of its
    # own in a cell four share, so each must be a target clear of the other.
    "detail-cut": Example(
        '{% card element="div" %}{% facts layout="band" %}{% fact "Example version" icon="layers" '
        'cut="registry.example.net/example-builds/example-app-server" %}<code>1.2.3</code>{% detail %}'
        '<small>{% muted tip="sha256:0123abcd" %}Pinned by digest{% endmuted %}{% dot %}built an example week ago</small>'
        '{% endfact %}{% fact "Example update" icon="deploy" %}Up to date{% detail %}<small>No newer version.</small>'
        "<small>Last read an example hour ago</small>{% endfact %}"
        '{% fact "Example state" icon="activity" %}Up{% detail %}<small>Passing</small>{% endfact %}'
        '{% fact "Example risk" icon="shield" %}None known{% detail %}<small>Read an example hour ago</small>'
        "{% endfact %}{% endfacts %}{% endcard %}"
    ),
    # A domain's overview as the dashboard draws it, its lead figure with the
    # line of its recent readings beside it.
    "figure-trend": Example(
        '{% layout %}{% section "Example overview" %}'
        '{% facts layout="band" size="lead" items=items %}{% endsection %}{% endlayout %}',
        lambda: {
            "items": [
                Kpi(
                    "Example balance", "$12,480", "Across every example account", drawing=Trend((9.0, 11.0, 10.0, 12.5))
                ),
                Kpi("Example income", "$4,200"),
                Kpi("Example spending", "$3,100"),
                Kpi("Example saved", "26%"),
            ]
        },
    ),
    # A whole's figure as a section leads with it, the line under it saying
    # how it moved in its tone, its parts under it with their legend, and
    # the changes beside it each in the tone of the way it went.
    "figure-toned": Example(
        '{% section "Example net worth" %}{% facts layout="band" size="lead" items=items %}{% endsection %}',
        lambda: {
            "items": [
                Kpi(
                    "Example net worth",
                    "$15,500.00",
                    "+$1,240.00 in the last 30 days",
                    url="/example/accounts/",
                    detail_tone=Tone.GOOD,
                    drawing=AllocationBar(
                        "Example assets by kind",
                        (
                            Share("Cash", 4200, 1, url="/example/?type=cash", detail="$4,200.00"),
                            Share("Investments", 12800, 3, url="/example/?type=investment", detail="$12,800.00"),
                            Share("Owed", 1500, 2, url="/example/?type=owed", detail="-$1,500.00", tone=Tone.ATTENTION),
                        ),
                    ),
                ),
                Kpi("Since yesterday", "+$120.00", tone=Tone.GOOD),
                Kpi("Available credit", "$8,000.00", tone=Tone.NEUTRAL),
                Kpi("Unrealized gain", "−$40.00", tone=Tone.ATTENTION),
            ]
        },
        premise=(_WHOLE, ".fact"),
    ),
    # Figures in a row of their own, as a report's year draws them: a line
    # that counts records leading to them, a figure that is its page's link,
    # one at zero, and a mark before a label.
    "figure-linked-line": Example(
        '{% card element="div" %}{% facts layout="band" size="large" items=items %}{% endcard %}',
        lambda: {
            "items": [
                Kpi("Example expenses", "$1,204.50", "12 example expenses", detail_url="/example/expenses/"),
                Kpi(Counted(3, "Example doc due", "Example docs due"), url="/example/docs/", icon="page"),
                Kpi(Counted(0, "Example asset", "Example assets"), detail="$0.00 total"),
            ]
        },
    ),
    # Facts ({% facts %}) in each layout and with each option; a new layout
    # or option of the tags comes with an entry here (CatalogTests in test_part_tags). A
    # zero beside a linked count stands on its value's line, as the Agents
    # card's counts do.
    "band": Example(
        '{% card element="div" %}{% facts layout="band" label="Example status" %}'
        '{% fact "Example rules" value=0 %}'
        '{% fact "Example held for approval" value=2 url="/example/?held=1" tone="attention" %}'
        '{% fact "Example refused in the last day" value=13 url="/example/?refused=1" detail="An example detail" %}'
        "{% endfacts %}{% endcard %}"
    ),
    "band-icons": Example(
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example running" icon="container" %}12{% detail %}<small>on 3 example machines</small>{% endfact %}'
        '{% fact "Example version" icon="layers" %}<code>example-1.2.3</code>{% detail %}'
        "<small>example.com/example/image</small><small>Tag only</small>{% endfact %}"
        '{% fact "An example label long enough to wrap onto a second line in its cell" icon="shield" tone="serious" %}'
        '<a href="/example/">{{ "2\u00a0example findings" }}</a>{% detail %}<small>An example detail</small>{% endfact %}'
        '{% fact "Example checks" icon="check-circle" tone="good" value="Passing" %}'
        "{% endfacts %}{% endcard %}"
    ),
    # Six facts, one of them a pin's change from one version to the next, as
    # a container's Upgrade card draws them: each version stands whole on its
    # line, the band counting its rows on a floor as wide as one.
    "band-pins": Example(
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example request" icon="commit" value="#58" %}'
        '{% fact "Example pin" icon="deploy" %}'
        '{% include "partials/_change.html" with from="2024.11.3-example-ls45" to="2024.12.1-example-ls48" code=True only %}'
        "{% endfact %}"
        '{% fact "Example fixes" icon="shield" value="None known" %}'
        '{% fact "Example brings" icon="info" value="Nothing serious" %}'
        '{% fact "Example data" icon="layers" value="1 mount" %}'
        '{% fact "Example check" icon="check-circle" value="Its health check" %}'
        "{% endfacts %}{% endcard %}"
    ),
    # An identifier longer than its fact's bound beside a plain value, then
    # one linked outside, as a container's Version and Update facts draw
    # them: each wraps a chip to a line and stands on its row's baseline.
    "band-code": Example(
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example running" icon="activity" value="Up 1 week" %}'
        '{% fact "Example version" icon="layers" %}<code>{{ tag }}</code>{% endfact %}'
        "{% endfacts %}{% endcard %}"
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example running" icon="activity" value="Up 1 week" %}'
        '{% fact "Example update" icon="deploy" %}{% entity url|outside label=tag code=True %}{% endfact %}'
        "{% endfacts %}{% endcard %}",
        {"tag": "2024.11.3-example2404-ls45-example-build", "url": "https://example.com/releases/2024.11.3"},
    ),
    # A band of one kind of value, as a machine's load is drawn: each a plain
    # reading under its mark, with a line saying what it is of.
    "band-readings": Example(
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example containers" icon="container" value="9 of 9 running" detail="1 has an update" %}'
        '{% fact "Example memory" icon="activity" value="48%" detail="3.8 GB of 8.0 GB" %}'
        '{% fact "Example storage" icon="layers" value="38.2 GB" detail="Layers, volumes and build cache" %}'
        '{% fact "Example uptime" icon="clock" value="12 days" detail="Since Nov 5" %}'
        "{% endfacts %}{% endcard %}"
    ),
    # A page's headline figures (`size="large"`), two drawn as well as said,
    # standing alone and in a section's frame.
    "band-figures": Example(
        '{% card element="div" %}{% facts layout="band" size="large" label="Example estate" %}'
        '{% fact "Example hosts online" icon="server" value="3 of 5" detail="2 example hosts offline" drawing=hosts %}'
        '{% fact "Example runs" icon="activity" value="4 of 6" detail="The last six example runs" drawing=runs %}'
        '{% fact "Example certificate" icon="lock" value="24 days" detail="example-com · Dec 11" %}'
        '{% fact "Example connections" icon="network" value=12 url="/example/" %}'
        "{% endfacts %}{% endcard %}"
        '{% section "Example estate" %}{% facts layout="band" size="large" %}'
        '{% fact "Example hosts online" icon="server" value="3 of 5" detail="2 example hosts offline" drawing=hosts %}'
        '{% fact "Example runs" icon="activity" value="4 of 6" detail="The last six example runs" drawing=runs %}'
        '{% fact "Example certificate" icon="lock" value="24 days" detail="example-com · Dec 11" %}'
        '{% fact "Example connections" icon="network" value=12 url="/example/" %}'
        "{% endfacts %}{% endsection %}",
        lambda: {"hosts": Dots.of(3, 5), "runs": runs()},
    ),
    # A range beside its unit, and a phrase, in a cell held narrow, as the
    # three under a lead hold it on a phone: each wraps, its lines clear of each other.
    # The values come as a view hands them, so the filter reads them as text:
    # a literal in a template is already markup, which it passes as it is.
    "band-wrapped": Example(
        '<div style="inline-size: 80px">{% facts layout="band" size="large" %}'
        '{% fact "Example range" value=range %}{% fact "Example phrase" value=phrase %}'
        "{% endfacts %}</div>",
        {"range": "10.5–12.5 mi", "phrase": "up to 2 a week"},
        premise=(_ONE_LINE, ".fact-value"),
    ),
    # A detail given as lines, one of them holding a link among its words.
    "band-lines": Example(
        '{% card element="div" %}{% facts layout="band" %}'
        '{% fact "Example certificate" value="example-com" url="/example/" detail=lines icon="lock" %}'
        '{% fact "Example health" icon="activity" tone="good" %}Passing{% detail %}'
        '<small>Forwards to example-app on <a href="/example/host/">example-host-01</a>.</small>'
        "<small>Read an example minute ago</small>{% endfact %}"
        "{% endfacts %}{% endcard %}",
        {"lines": ("Example issuer", "24 example days left")},
    ),
    # A card's summary line, as the dashboard's counts across HQ draw it: a
    # zero, a linked count, a toned count, one with its detail, a count of one
    # saying its label for one, enough to wrap at a phone's width.
    "inline": Example(
        '{% card element="div" %}{% facts layout="inline" label="Example summary" %}'
        "{% for card in cards %}{% fact card.label value=card.value url=card.url detail=card.detail tone=card.status %}"
        "{% endfor %}{% endfacts %}{% endcard %}",
        lambda: {
            "cards": checked_cards(
                "example",
                (
                    {"id": "rules", "label": Counted(0, "Example rule", "Example rules"), "url": "/example/"},
                    {
                        "id": "waiting",
                        "label": Counted(1, "Example device waiting", "Example devices waiting"),
                        "url": "/example/?waiting=1",
                        "status": Tone.SERIOUS,
                    },
                    {
                        "id": "updates",
                        "label": Counted(2, "Example open update", "Example open updates"),
                        "url": "/example/updates/",
                        "detail": "2 example alerts with no update open",
                    },
                    {
                        "id": "drafts",
                        "label": Counted(1, "Example draft", "Example drafts"),
                        "url": "/example/drafts/",
                        "detail": "22 published",
                    },
                    {
                        "id": "expenses",
                        "label": "Example expenses",
                        "value": "$1,204.50",
                        "url": "/example/expenses/",
                        "detail": "All deductible est.",
                    },
                ),
            )
        },
        premise=(_WHOLE, ".fact"),
    ),
    # A few short pairs, one of them a long address that wraps in its own
    # value's track as the pairs go across the page.
    "list": Example(
        '{% facts layout="list" %}'
        '{% fact "Address" %}<code>https://hq.example.com/mcp/</code>{% endfact %}'
        '{% fact "Sign-in" %}<a href="/example/">{{ sign_in }}</a>{% endfact %}'
        '{% fact "Protocol" %}<code>2025-06-18</code>{% endfact %}'
        "{% endfacts %}",
        {"sign_in": "https://auth.example.com/application/o/example-hq-agent/.well-known/openid-configuration"},
    ),
    # Identifiers that wrap in a list held narrow, code, a bare path, an
    # entity's name and one linked outside, each under a fact of one line:
    # every value stands on its label's line, clear of the fact above it.
    "list-identifiers": Example(
        '<div style="max-inline-size: 300px">{% facts %}'
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example image" %}<code>{{ image }}</code>{% endfact %}'
        '{% fact "Example kind" value="Renewal" %}'
        '{% fact "Example path" value=path %}'
        '{% fact "Example port" value="8123" %}'
        '{% fact "Example serves" %}{% entity service %}{% endfact %}'
        '{% fact "Example state" value="Running" %}'
        '{% fact "Example update" %}{% entity url|outside label=tag code=True %}{% endfact %}'
        "{% endfacts %}</div>",
        lambda: {
            "image": "registry.example.com/example-team/example-image:2024.11.3-example-build",
            "path": "example-vault/renewals/example-renewal-with-a-long-name.md",
            "service": EntityLink(
                label="example-automation-dashboard.example.com", url="/example/", kind_label="Service"
            ),
            "tag": "2024.11.3-example2404-ls45-example-build",
            "url": "https://example.com/releases/2024.11.3",
        },
        premise=(_ONE_LINE, ".fact:nth-child(even) > .fact-value"),
    ),
    # Short facts in a section across the page, one with its detail under
    # its value, as a proposal's commit is drawn: the pairs go across the
    # frame rather than down its start, the last row's pairs spanning what it
    # leaves. Then seven, which wrap onto rows whose last pair spans its row.
    "list-across": Example(
        '{% section "Example change" %}{% facts %}'
        '{% fact "Commit" %}<code>0123456789ab</code>{% detail %}<small>on main</small>{% endfact %}'
        '{% fact "Committer" value="example-maintainer" %}'
        '{% fact "Signature" value="Verified by an example" %}'
        '{% fact "Scanned" value="Nov 11, 10:31 PM" %}'
        "{% endfacts %}{% endsection %}"
        '{% section "Example record 2" %}{% facts %}'
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example address" value="192.0.2.10" detail="Read an example minute ago" %}'
        '{% fact "Example kind" value="Container" %}'
        '{% fact "Example owner" value=none %}'
        '{% fact "Example port" value="8123" %}'
        '{% fact "Example state" value="Running" %}'
        '{% fact "Example image" %}<code>registry.example.com/example-team/example-image:2024.11.3</code>{% endfact %}'
        "{% endfacts %}{% endsection %}",
        {"none": None},
    ),
    "list-many": Example(
        "{% facts %}"
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example address" value="192.0.2.10" detail="Read an example minute ago" %}'
        '{% fact "Example owner" value=none %}'
        '{% fact "Example count" value=0 %}'
        '{% fact "Example link" value="example.com" url="/example/" %}'
        '{% fact "An example label long enough to take its column\'s room" value="Example value" %}'
        '{% fact "Example list" %}{% list kind="dots" %}<li>example-one</li><li>example-two</li>'
        "<li>example-three</li>{% endlist %}{% endfact %}"
        '{% fact "Example prose" %}An example value that is a sentence long enough to wrap onto a second '
        "line at the narrower widths, as a reading's note does.{% endfact %}"
        "{% endfacts %}",
        {"none": None},
    ),
    # A run of links long enough to wrap, as a record's relationships list
    # what each phrase reaches: every line a target tall.
    "list-links": Example(
        "{% facts %}"
        '{% fact "Example forwards to" %}{% list kind="dots" %}'
        '<li><a href="/example/one/">example-app-one</a></li><li><a href="/example/two/">example-app-two</a></li>'
        '<li><a href="/example/three/">example-app-three</a></li><li><a href="/example/four/">example-four</a></li>'
        '<li><a href="/example/five/">example-five</a></li><li><a href="/example/six/">example-app-six</a></li>'
        "{% endlist %}{% endfact %}"
        "{% endfacts %}"
    ),
    # A list on the page and one in a disclosure under it, as a resource's
    # other settings are drawn: the list opened to read more sits closer.
    "list-compact": Example(
        '{% section "Example settings" %}{% facts %}'
        '{% fact "Example name" value="example-01" %}'
        '{% fact "Example image" %}<code>{{ image }}</code>{% endfact %}'
        "{% endfacts %}"
        '{% section "Example other settings" fold=True open=True %}{% facts %}'
        '{% fact "Example port" value="8123" %}'
        '{% fact "Example restart" value="unless-stopped" %}'
        '{% fact "Example network" %}<code>example-network</code>{% endfact %}'
        "{% endfacts %}{% endsection %}{% endsection %}",
        {"image": "registry.example.com/example-team/example-image:2024.11.3"},
        premise=(_TIGHTER, ".facts-list"),
    ),
    # A band of facts beside a page's title, as the dashboard's glance draws it.
    "band-glance": Example(
        '{% include "partials/_page_head.html" %}{% section "Example section" %}<p>An example line.</p>{% endsection %}',
        lambda: {"page": Page(title="Example glance"), "page_aside": _Drawn(_GLANCE), "hours": _hours()},
        premise=(_WHOLE, ".fact"),
        heads_the_page=True,
    ),
    # The same band on its own line, as a narrower head draws it, under the dashboard's day and its note.
    "band-glance-under-the-title": Example(
        '{% include "partials/_page_head.html" %}{% section "Example section 2" %}<p>An example line.</p>{% endsection %}',
        lambda: {
            "page": Page(title="Example glance", lede="An example lede long enough to take the head's line."),
            "page_dateline": date(2026, 11, 18),
            "page_visit": PageVisit("page", "dashboard"),
            "page_facts": "An example fact · another example fact",
            "page_aside": _Drawn(_GLANCE_EMPTY),
        },
        heads_the_page=True,
    ),
    # Two readings by the title stand together, a column each as wide as
    # four would take.
    "band-glance-two": Example(
        '{% include "partials/_page_head.html" %}{% section "Example section 3" %}<p>An example line.</p>{% endsection %}',
        lambda: {"page": Page(title="Example glance"), "page_aside": _Drawn(_GLANCE_TWO)},
        heads_the_page=True,
    ),
    # A strip of small figures, more than one line holds in a narrow box.
    "band-small": Example(
        '<div style="inline-size: 240px">{% facts layout="band" size="small" items=hours label="Example hours" %}</div>',
        lambda: {"hours": _hours()},
    ),
}
