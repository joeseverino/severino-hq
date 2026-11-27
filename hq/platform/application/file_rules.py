"""The rules a template, stylesheet, script or module keeps on its own, each judged one file at a time.

One declaration for both doors: the host's quick gate (``lint:file-rules``)
hands it the files a change touched, and the extension check
(``plugin_validation``) reads a package's whole tree. Each rule carries the
cases it refuses and passes, which one test walks.

A file is named as Django loads it when it sits under a ``templates``
directory (``partials/_empty_state.html``), and by its path from the root
otherwise (``static/css/app/page.css``), so a rule reads an extension's
templates as it reads the host's.
"""

import argparse
import ast
import functools
import re
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

lazy import django
lazy from django.apps import apps
lazy from django.utils.text import Truncator

lazy from hq.platform.core.interface_text import TEMPLATE_COMMENT

Finds = Callable[[str], Iterable[tuple[int, str]]]

# What no rule reads: a vendored copy, a build, an installed tree.
IGNORED = ("**/vendor/**", "**/node_modules/**", "**/*.min.js")
# This module spells each refused form in its cases.
HERE = "hq/platform/application/file_rules.py"
CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass(frozen=True)
class Rule:
    """A rule over one file: what it finds in a file's text, the files it
    reads and skips (globs over a file's name), and its cases."""

    fix: str
    reads: tuple[str, ...]
    finds: Finds
    refuses: tuple[str, ...]
    passes: tuple[str, ...] = ()
    skips: tuple[str, ...] = ()

    def holds(self, name: str) -> bool:
        path = PurePosixPath(name)
        return any(map(path.full_match, self.reads)) and not any(map(path.full_match, (*self.skips, *IGNORED)))


def _line(text: str, at: int) -> int:
    return text.count("\n", 0, at) + 1


def _blank(pattern: re.Pattern[str] | None, text: str) -> str:
    """``text`` with each match of ``pattern`` emptied but its line breaks, so lines still count."""

    return pattern.sub(lambda found: "\n" * found.group().count("\n"), text) if pattern else text


def _said(text: str) -> str:
    return Truncator(" ".join(text.split())).chars(100)


def matching(pattern: str, flags: int = 0, *, around: re.Pattern[str] | None = None) -> Finds:
    """A rule's finder: each match of ``pattern``, outside the comments ``around`` matches."""

    compiled = re.compile(pattern, flags)

    def finds(text: str) -> list[tuple[int, str]]:
        text = _blank(around, text)
        return [(_line(text, found.start()), _said(found.group())) for found in compiled.finditer(text)]

    return finds


def _template(finds: Callable[[str], Iterator[tuple[int, str]]]) -> Finds:
    """A finder over a template's text outside its comments."""

    return lambda text: list(finds(_blank(TEMPLATE_COMMENT, text)))


def _django() -> None:
    """Django's apps, for a rule whose words come from a model's module; configured from DJANGO_SETTINGS_MODULE."""

    if not apps.ready:
        django.setup()


# Templates ------------------------------------------------------------------

_EMPTY_STATE = re.compile(r'\{% include "partials/_empty_state\.html"(.*?)%\}', re.DOTALL)


@_template
def _empty_states(text: str) -> Iterator[tuple[int, str]]:
    for include in _EMPTY_STATE.finditer(text):
        if "compact=" in include[1] or not re.search(r"\b(?:message|absent)=", include[1]):
            yield _line(text, include.start()), _said(include.group())


_FORM = re.compile(r"<form\b(?P<attributes>[^>]*)>(?P<body>.*?)</form>", re.DOTALL | re.IGNORECASE)
_POSTS = re.compile(r"""\bmethod\s*=\s*["']?post\b""", re.IGNORECASE)


@_template
def _unkeyed_forms(text: str) -> Iterator[tuple[int, str]]:
    for form in _FORM.finditer(text):
        if _POSTS.search(form["attributes"]) and "{% write_token %}" not in form["body"]:
            yield _line(text, form.start()), _said(f"<form{form['attributes']}>")


# The script that answers `pagereveal`, which fires before the new page's
# first frame and so before a deferred script runs: it loads in the head
# without `defer`.
TRANSITION = "js/transitions.js"
BEFORE_DRAWING = (TRANSITION,)
_SCRIPT = re.compile(r"<script\b[^>]*\bsrc=[^>]*>")


def _blocking(text: str) -> Iterator[tuple[int, str]]:
    for script in _SCRIPT.finditer(text):
        if not re.search(r'\bdefer\b|type="module"', script.group()) and not any(
            name in script.group() for name in BEFORE_DRAWING
        ):
            yield _line(text, script.start()), _said(script.group())


def _unnonced(text: str) -> Iterator[tuple[int, str]]:
    for script in re.finditer(r"<script\b[^>]*>", text, re.IGNORECASE):
        if "{% csp_nonce_attr %}" not in script.group():
            yield _line(text, script.start()), _said(script.group())


# A script only one page uses loads on that page, keyed by the attribute that
# marks the element it binds, so a page drawing that element without it is
# found here rather than as a control that does nothing.
PAGE_SCRIPTS = {"js/topology.js": "data-topology"}
# The cases' page script.
_PAGE_SCRIPT, _HOOK = next(iter(PAGE_SCRIPTS.items()))


def _unloaded_page_scripts(text: str) -> Iterator[tuple[int, str]]:
    for script, hook in PAGE_SCRIPTS.items():
        drawn = re.search(rf"\s{re.escape(hook)}(?![\w-])", text)
        if drawn and script not in text:
            yield _line(text, drawn.start()), f"{hook} without {script}"


def _plot_literals(text: str) -> Iterator[tuple[int, str]]:
    from hq.platform.application.ui import PLOT_HEIGHT, PLOT_LEFT, PLOT_TOP, PLOT_WIDTH

    plotted = {str(round(value)) for value in (PLOT_LEFT, PLOT_TOP, PLOT_LEFT + PLOT_WIDTH, PLOT_TOP + PLOT_HEIGHT)}
    # Only the SVG geometry attributes: a viewBox is drawn from the chart's own width and height.
    for found in re.finditer(r'\b(x1|x2|y1|y2|cx|cy|width|height)="([0-9.]+)"', text):
        if found[2].split(".")[0] in plotted:
            yield _line(text, found.start()), found.group()


@functools.cache
def _status_class() -> re.Pattern[str]:
    """A class a status draws, and a state's tone spelled as a `control-` class: a state, a tone, or one filled in."""

    _django()
    from hq.platform.application.infrastructure import ResourceState
    from hq.platform.application.tone import Tone

    words = "|".join(sorted({*ResourceState, *Tone}))
    return re.compile(r"^(pill|toned|worth)(-|$)|^control-(\{\{|(" + words + r")$)")


def _statuses_by_hand(text: str) -> Iterator[tuple[int, str]]:
    for attribute in re.finditer(r"""\bclass\s*=\s*(["'])(.*?)\1""", text, re.DOTALL):
        for word in re.split(r"\s+|\{%|%\}", attribute[2]):
            if _status_class().match(word):
                yield _line(text, attribute.start()), word


_BAR = re.compile(r"\{% filter_bar(?P<options>.*?)%\}(?P<body>.*?)\{% endfilter_bar %\}", re.DOTALL)


@_template
def _counted_summaries(text: str) -> Iterator[tuple[int, str]]:
    for bar in _BAR.finditer(text):
        summary = bar["body"].partition("{% summary %}")[2]
        if re.search(r"\b(?:of|shown)\b", re.sub(r"\{.*?\}", "", summary, flags=re.DOTALL)):
            yield _line(text, bar.start()), _said(summary)


@_template
def _uncounted_bars(text: str) -> Iterator[tuple[int, str]]:
    for bar in _BAR.finditer(text):
        if "filtered=" in bar["options"] and "shown=" not in bar["options"]:
            yield _line(text, bar.start()), _said(bar.group())


# Stylesheets ----------------------------------------------------------------

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rules(text: str) -> Iterator[tuple[int, str, str]]:
    """Each innermost rule of a sheet outside its comments: its line, selector and declarations."""

    text = _blank(CSS_COMMENT, text)
    for rule in _RULE.finditer(text):
        selector = rule[1].lstrip()
        yield _line(text, rule.end(1) - len(selector)), " ".join(selector.split()), rule[2]


def _opaque_blurs(text: str) -> Iterator[tuple[int, str]]:
    for line, selector, body in _rules(text):
        if "backdrop-filter" in body:
            background = re.search(r"background(?:-color)?:\s*([^;]+);", body)
            if not (background and re.search(r"transparent|/\s*[.\d]+\)|rgba|hsla", background[1])):
                yield line, selector


# What makes a rule the fill: the content column taken edge to edge, or a box
# sized to the view under the header, for a page by what its column holds.
# Paper takes the column edge to edge for every page (cadence.css), which is
# no page's fill.
_FILLS = re.compile(r"max-width\s*:\s*none|(?<![-\w])padding\s*:\s*0\s*;|100dvh\s*-\s*var\(--site-header-height")


def _column_fills(text: str) -> Iterator[tuple[int, str]]:
    for line, selector, body in _rules(text):
        if (
            re.search(r"\.content\s*(?::has|>)", selector)
            and _FILLS.search(body)
            and "[data-document-page]" not in selector
        ):
            yield line, selector


def _document_fills(text: str) -> Iterator[tuple[int, str]]:
    for line, selector, body in _rules(text):
        if "[data-document-page]" in selector and _FILLS.search(body):
            yield line, selector


# A table's tracks after its row's tick-box: the one that names the rows, then the rest.
_TABLE_TRACKS = re.compile(r"grid-template-columns:\s*var\(--lead,\)\s*\[name\]\s*auto\s+repeat\((\d+),")


def _table_tracks(text: str) -> Iterator[tuple[int, str]]:
    from hq.platform.core.table_layout import TRACKS

    found = list(_TABLE_TRACKS.finditer(text))
    if [track[1] for track in found] != [str(TRACKS - 1)]:
        yield _line(text, found[0].start()) if found else 1, f"{len(found)} grids for {TRACKS} tracks"


# Modules --------------------------------------------------------------------


@functools.cache
def _procedure_names() -> re.Pattern[str]:
    _django()
    from hq.platform.core.example_estate.automation import PROCEDURES, REVISION

    named = {REVISION}
    for procedure in PROCEDURES:
        named |= {procedure["declaration"]["id"], procedure["playbook"], procedure["sidecar"]}
    return re.compile("|".join(map(re.escape, sorted(named))))


def _procedures_named(text: str) -> Iterator[tuple[int, str]]:
    for found in _procedure_names().finditer(text):
        yield _line(text, found.start()), found.group()


def _lifecycle_restated(text: str) -> Iterator[tuple[int, str]]:
    """A collection listing two of the lifecycle's states, its items or a
    dictionary's keys, or a string, f-string parts and joined literals
    whole, that spells the unreported marker."""

    from hq.platform.application.connection_lifecycle import UNREPORTED_FACT, UNREPORTED_NODE, Lifecycle

    states = {str(state) for state in Lifecycle}
    label = Lifecycle.UNREPORTED.label.lower()
    try:
        tree = ast.parse(text)
    except SyntaxError:  # Ruff reports a module that does not parse
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.Tuple)):
            items = node.keys if isinstance(node, ast.Dict) else node.elts
            listed = sorted({item.value for item in items if isinstance(item, ast.Constant) and item.value in states})
            if len(listed) > 1:
                yield node.lineno, ", ".join(listed)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (node.value.lower() == label or UNREPORTED_FACT in node.value or UNREPORTED_NODE in node.value)
        ):
            yield node.lineno, _said(node.value)


_HOST_MODULES = ("hq/**/*.py",)
_HOST_LEFT_OUT = ("hq/**/tests/**", "hq/**/migrations/**", HERE)

RULES: dict[str, Rule] = {
    "empty-block": Rule(
        "A list says what empty means with table_empty, never by overriding {% block empty %}",
        ("**/*.html",),
        matching(r"\{% block empty %\}", around=TEMPLATE_COMMENT),
        refuses=("{% block empty %}<p>None.</p>{% endblock %}",),
        passes=("{# never {% block empty %} #}",),
    ),
    "empty-state": Rule(
        "An empty state is one line and one remedy: give it message= or absent=, never compact=,"
        " since the surface frames it",
        ("**/*.html",),
        _empty_states,
        refuses=(
            '{% include "partials/_empty_state.html" with message="No scan yet." compact=True %}',
            '{% include "partials/_empty_state.html" with\n  remedy=x %}',
        ),
        passes=('{% include "partials/_empty_state.html" with message="No scan yet." %}',),
    ),
    "identity-provider": Rule(
        "Say 'your sign-in': HQ works with any identity provider",
        ("**/*.html",),
        matching(r"Pocket ID", around=TEMPLATE_COMMENT),
        refuses=("<p>Signed in through Pocket ID.</p>",),
        passes=("{# Pocket ID #}<p>Signed in.</p>",),
    ),
    "form-errors": Rule(
        'Include "partials/_form_errors.html", which prints a form\'s own errors',
        ("**/*.html",),
        matching(r"\.non_field_errors"),
        refuses=("{% for error in form.non_field_errors %}{{ error }}{% endfor %}",),
        passes=('{% include "partials/_form_errors.html" with form=form %}',),
        skips=("partials/_form_errors.html",),
    ),
    "tone-tag": Rule(
        "Give the fact's projection a tone and write tone=<it> on {% fact %}",
        ("**/*.html",),
        matching(r"\{%\s*tone\s*%\}"),
        refuses=('{% fact "State" %}{% tone %}good{% endfact %}',),
        passes=('{% fact "State" value tone=state.tone %}',),
    ),
    "outside-link": Rule(
        "Write {% entity url|outside label=name %} or {% outside_link %}, which open a page outside HQ",
        ("**/*.html",),
        matching(r'<a\b[^>]*(?:href="https?:|rel="noopener|target="_blank"|class="external-link)'),
        refuses=('<a href="https://example.com/">Example</a>', '<a href="{{ url }}" target="_blank">Run</a>'),
        passes=('<a href="{% url "projects:list" %}">Projects</a>',),
        skips=("partials/_outside_link.html",),
    ),
    "script-deferred": Rule(
        "Defer the script: only the one answering before the first frame (BEFORE_DRAWING) loads without waiting",
        ("**/*.html",),
        _blocking,
        refuses=("<script src=\"{% static 'js/app.js' %}\" {% csp_nonce_attr %}></script>",),
        passes=(
            "<script src=\"{% static 'js/app.js' %}\" defer {% csp_nonce_attr %}></script>",
            "<script src=\"{% static 'js/transitions.js' %}\" {% csp_nonce_attr %}></script>",
        ),
    ),
    "script-nonce": Rule(
        "Write {% csp_nonce_attr %} on the script: the policy's script-src is the response's nonce"
        " and 'strict-dynamic' (settings.SECURE_CSP), so the browser refuses a script without it",
        ("**/*.html",),
        _unnonced,
        refuses=("<SCRIPT src=\"{% static 'js/app.js' %}\" defer></SCRIPT>",),
        passes=("<script src=\"{% static 'js/app.js' %}\" defer {% csp_nonce_attr %}></script>",),
    ),
    "inline-script": Rule(
        "Load the script from a file and bind its events there: the policy refuses inline script",
        ("**/*.html",),
        matching(r"<script(?![^>]*\bsrc=)|\son[a-z]+\s*=", re.IGNORECASE),
        refuses=("<script {% csp_nonce_attr %}>go()</script>", '<button onclick="go()">Go</button>'),
        passes=("<script src=\"{% static 'js/app.js' %}\" defer {% csp_nonce_attr %}></script>",),
    ),
    "page-script": Rule(
        "Load the page's script where its element is drawn (PAGE_SCRIPTS): base.html loads none of them",
        ("**/*.html",),
        _unloaded_page_scripts,
        refuses=(f'<div {_HOOK}="{{{{ graph }}}}"></div>',),
        passes=(
            f"<div {_HOOK}></div><script src=\"{{% static '{_PAGE_SCRIPT}' %}}\" defer></script>",
            f"<div {_HOOK}-node></div>",
        ),
    ),
    "row-more-block": Rule(
        "Show a block in a row's detail as row-below, the line beneath the row at the table's width:"
        " row-more is one cell's word or list item",
        ("**/*.html",),
        matching(r'<(div|ul|ol|p|section|form|table|dl)\b[^>]*class="[^"]*\brow-more\b'),
        refuses=('<ul class="row-more">',),
        passes=('<span class="row-more">', '<div class="row-below">'),
    ),
    "plot-literal": Rule(
        "Read the plot's coordinates from the chart (ui.py), never restate them in its template",
        ("partials/*chart*.html",),
        _plot_literals,
        refuses=('<line x1="48" x2="702" y1="{{ y }}" y2="{{ y }}"/>',),
        passes=('<line x1="{{ chart.left }}" x2="{{ chart.right }}"/>', '<rect width="4" height="4"/>'),
    ),
    "reading-age": Rule(
        'Say a reading\'s age through {% include "partials/_reading_age.html" %}, so it reads alike on every page',
        ("**/*.html",),
        matching(r"\bread\b[^{<]{0,4}\{\{[^}]*\|(?:when|when_text)\b", re.IGNORECASE),
        refuses=("Last read {{ at|when }}",),
        passes=('{% include "partials/_reading_age.html" with at=at %}',),
        skips=("partials/_reading_age.html",),
    ),
    "write-key": Rule(
        "Carry {% write_token %} in place of {% csrf_token %}, so a form sent twice acts once",
        ("**/*.html",),
        _unkeyed_forms,
        refuses=(
            '<form method="post">{% csrf_token %}</form>',
            '<form method=POST action="/x/">\n{% csrf_token %}\n</form>',
        ),
        passes=('<form method="post">{% write_token %}</form>', '<form method="get"></form>'),
        # A sign-in changes nothing a capability runs, so it keeps the CSRF field alone.
        skips=("auth/login.html",),
    ),
    "status-by-hand": Rule(
        "Draw these with {% status ... %} (pill=False for a toned figure)",
        ("**/*.html",),
        _statuses_by_hand,
        refuses=('<span class="pill tone-good">Up</span>', '<b class="control-{{ state }}">x</b>'),
        passes=('{% status state %}<span class="muted">x</span>',),
    ),
    "table-derived": Rule(
        "The declared table derives a figure's alignment and its sort control; drop them from the template",
        ("**/*.html",),
        matching(r"\|\s*sortable\b|\bnum-col\b"),
        refuses=('<th class="num-col">Total</th>', "{{ column|sortable }}"),
        passes=("{% data_table table %}",),
    ),
    "filter-bar-summary": Rule(
        "A filter bar's count is the part's: give the bar shown= and total=, never a count in the summary",
        ("**/*.html",),
        _counted_summaries,
        refuses=('{% filter_bar search="Find" %}{% summary %}{{ n }} of {{ m }}{% endfilter_bar %}',),
        passes=(
            (
                '{% filter_bar search="Find" filtered=x clear=y shown=n total=m %}{% choices %}'
                '{% select "k" kinds every="kind" %}{% summary %}{{ k|counted:"needs a look,need a look" }}.'
                "{% endfilter_bar %}"
            ),
        ),
    ),
    "filter-bar-count": Rule(
        "A filter bar that narrows says how many it shows: give it shown= and total=",
        ("**/*.html",),
        _uncounted_bars,
        refuses=('{% filter_bar search="Find" filtered=x clear=y %}{% endfilter_bar %}',),
        passes=('{% filter_bar search="Find" filtered=x clear=y shown=n total=m %}{% endfilter_bar %}',),
        # Its count is its pager's, which the list's table draws under it.
        skips=("partials/_table_toolbar.html",),
    ),
    "form-card": Rule(
        'A page that is one form is {% extends "form_page.html" %}, whose frame draws its card',
        ("**/*.html",),
        matching(r'data_frame="form"', around=TEMPLATE_COMMENT),
        refuses=('{% include "partials/_form_card.html" with data_frame="form" %}',),
        passes=('{% extends "form_page.html" %}',),
        skips=("form_page.html",),
    ),
    "document-footer": Rule(
        "A page that is one document extends document_page.html, the one template that empties the footer",
        ("**/*.html",),
        matching(r"{%\s*block\s+site_footer\s*%}\s*{%\s*endblock"),
        refuses=("{% block site_footer %}{% endblock %}",),
        passes=('{% extends "document_page.html" %}',),
        skips=("document_page.html",),
    ),
    "document-fill": Rule(
        "A page that is one document takes the fill of [data-document-page] (page.css)",
        ("**/*.css",),
        _column_fills,
        refuses=(".content:has(.api-reference) { max-width: none; }",),
        passes=(".content > .card { gap: 1rem; }", "/* .content > x { padding: 0; } */"),
    ),
    "document-fill-sheet": Rule(
        "The fill of [data-document-page] is page.css's one rule",
        ("**/*.css",),
        _document_fills,
        refuses=("[data-document-page] .content { padding: 0; }",),
        passes=("[data-document-page] h1 { margin: 0; }",),
        skips=("static/css/app/page.css",),
    ),
    "opaque-blur": Rule(
        "A backdrop filter costs a layer redrawn on every scroll, and shows nothing behind an opaque"
        " background: let the page through (transparent mixed in, or a color with alpha)",
        ("**/*.css",),
        _opaque_blurs,
        refuses=(".bar { backdrop-filter: blur(8px); background: var(--surface); }", ".bar { backdrop-filter: none; }"),
        passes=(
            ".bar { backdrop-filter: blur(8px); background: color-mix(in srgb, var(--surface), transparent 20%); }",
        ),
    ),
    "table-tracks": Rule(
        "A row's grid is its table's and DataTable refuses more columns than TRACKS: the grid holds that many",
        ("static/css/**/tables.css",),
        _table_tracks,
        refuses=(".data-table { grid-template-columns: var(--lead,) [name] auto repeat(3, auto); }",),
        passes=(".data-table { grid-template-columns: var(--lead,) [name] auto repeat(15, auto); }",),
    ),
    # Scripts ----------------------------------------------------------------
    "page-transition": Rule(
        f"Answer the page transition in {TRANSITION} alone, the one script that loads in time for it",
        ("**/*.js",),
        matching(r"[\"']page(reveal|swap)[\"']"),
        refuses=('window.addEventListener("pagereveal", reveal);',),
        passes=('window.addEventListener("pageshow", shown);',),
        skips=(f"**/{TRANSITION}",),
    ),
    "table-script-measures": Rule(
        "A table is laid out by its stylesheet alone (tables.css): its script measures nothing,"
        " watches no resize and writes no style",
        ("**/js/tables.js",),
        matching(
            r"getBoundingClientRect|getClientRects|offset(Width|Height)|client(Width|Height)|scroll(Width|Height)"
            r"|ResizeObserver|\.style\b|setProperty"
        ),
        refuses=("const wide = table.offsetWidth;", "row.style.height = '2rem';"),
        passes=('row.toggleAttribute("data-row-opened");',),
    ),
    # Modules and templates --------------------------------------------------
    "offer-route": Rule(
        "Reach a declaration's edit or removal through the command its view runs (pages.command_reach),"
        " never by the route's name",
        (*_HOST_MODULES, "**/*.html"),
        matching(r"""["']control_plane:(?:edit|remove)["']"""),
        refuses=('reverse("control_plane:edit", kwargs={"key": key})', "{% url 'control_plane:remove' key %}"),
        passes=('reverse("control_plane:detail", kwargs={"key": key})',),
        # A benchmark visits each page, and the example estate records the door its rows came through.
        skips=(
            *_HOST_LEFT_OUT,
            "hq/platform/core/management/commands/bench_pages.py",
            "hq/platform/core/example_estate/history.py",
        ),
    ),
    "seeded-procedure": Rule(
        "The host draws a playbook from the adopted catalog alone: name none of the example estate's",
        (*_HOST_MODULES, "**/*.html"),
        _procedures_named,
        refuses=('OPERATION = "example.cache_flush"',),
        passes=("OPERATION = manifest.id",),
        skips=(*_HOST_LEFT_OUT, "hq/platform/core/example_estate/**"),
    ),
    "lifecycle-vocabulary": Rule(
        "A connection's lifecycle is said in connection_lifecycle.py: read its states and marker there",
        _HOST_MODULES,
        _lifecycle_restated,
        refuses=(
            'LABELS = {"reachable": "Up", "stale": "Old"}',
            'WORKING = "unprobed", "reachable"',
            '"""Marks connection-unreported."""',
            'SAID = f"{name}NO LONGER REPORTED"',
            'NODE = "conne" "ction:unreported:"',
        ),
        passes=('ONE = ("reachable", "other")', "STATE = Lifecycle.REACHABLE"),
        skips=(*_HOST_LEFT_OUT, "hq/platform/application/connection_lifecycle.py"),
    ),
}


def _name(relative: PurePosixPath) -> str:
    """A file's name for the rules: under a ``templates`` directory, the name Django loads it by."""

    parts = relative.parts
    if "templates" in parts[:-1]:
        at = len(parts) - 1 - parts[::-1].index("templates")
        return PurePosixPath(*parts[at + 1 :]).as_posix()
    return relative.as_posix()


def _files(named: Path) -> Iterator[Path]:
    """``named``, or each file under it outside hidden directories (a virtualenv, git's own)."""

    if not named.is_dir():
        yield named
        return
    for top, directories, files in named.walk():
        directories[:] = [name for name in directories if not name.startswith(".")]
        yield from (top / name for name in files)


def violations(source_root: str | Path, *within: str | Path) -> dict[str, list[str]]:
    """Each rule's ``path:line: found`` lines, by rule, over the files ``within``
    names under ``source_root`` (a directory: each file under it), by default all."""

    root = Path(source_root).resolve()
    paths = sorted({path for named in map(Path, within or (root,)) for path in _files(named.resolve())})
    found: dict[str, list[str]] = {}
    for path in paths:
        relative = PurePosixPath(path.relative_to(root).as_posix())
        held = [(key, rule) for key, rule in RULES.items() if rule.holds(_name(relative))]
        try:
            text = path.read_text(encoding="utf-8") if held else ""
        except UnicodeDecodeError:  # noqa: S112 - an image or font kept beside the templates is no source
            continue
        for key, rule in held:
            found.setdefault(key, []).extend(f"{relative}:{line}: {what}" for line, what in rule.finds(text))
    return {key: lines for key, lines in found.items() if lines}


def report(source_root: str | Path, *within: str | Path) -> bool:
    """Print each rule's violations under its fix; whether there were any."""

    found = violations(source_root, *within)
    for key, lines in found.items():
        print(f"{RULES[key].fix} ({key}):", *(f"- {line}" for line in lines), sep="\n")
    return bool(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source_root", type=Path)
    parser.add_argument("files", nargs="*", type=Path, help="Only these, as the quick gate hands it a change's files")
    args = parser.parse_args()
    return int(report(args.source_root, *args.files))


if __name__ == "__main__":
    sys.exit(main())
