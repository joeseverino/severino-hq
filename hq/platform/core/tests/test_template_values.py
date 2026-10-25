"""Every value a template prints as text holds the names in it.

A browser may break a line after any hyphen, so "example-docker-host-01"
printed bare can split into "example-docker-" and "host-01". `|breakable`
holds a name's parts and leaves a plain word as it is, so it is safe on any
value; a value printed as text ends in it, or in a filter that holds its
names itself. The browser rule `a_token_breaks_only_where_it_may` sees only
the names its fixtures hold, at the widths it renders; this reads every
template.
"""

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from django import forms
from django.conf import settings
from django.db import models
from django.template.base import Lexer, TokenType, filter_re
from django.test import SimpleTestCase, tag
from django.utils.html import format_html

from hq.platform.application.entity_links import EntityLink
from hq.platform.application.tests.markup import elements, select
from hq.platform.core.templatetags.value_tags import breakable, entity, readable, when

# Filters whose output holds every name in it: `breakable` itself, and the
# filters that build on it or print a `<time>`.
HOLDING = {"breakable", "or_empty", "readout", "when", "readable"}

# Filters whose output can never hold a hyphen or a seam, so it has nothing
# to hold.
WORDS_ONLY = {
    "counted": "a count and the words for it",
    "bytes": "a byte count",
    "money": "an amount",
    "seconds": "a run time",
    "span": "a length of time",
    "ago": "an age in words",
    "ago_short": "an age in words",
    "day": "a day in words",
    "expiry": "a date and a count of days",
    "length": "a number",
    "floatformat": "a number",
    "intcomma": "a number",
    "date": "a date",
    "time": "a time",
    "pluralize": "a suffix",
    "signed_in": "an age in words",
    "timesince": "a length of time",
    "timeuntil": "a length of time",
    "naturaltime": "an age in words",
    "urgency": "one of the words a pill says",
    "json_script": "a script element, read by a script and never shown",
}

# Filters that keep the markup they are given, so a value held before one
# stays held.
KEEPS_MARKUP = {"linebreaksbr"}

# Values that are markup by the time a template prints them, built by Django
# or by their author: `|breakable` passes markup as it is.
MARKUP = {"block.super": "the parent template's block"}
MARKUP_ATTRIBUTES = {
    "errors": "a form's error list, as Django renders it",
    "non_field_errors": "a form's error list, as Django renders it",
}
MARKUP_FILTERS = {"safe": "markup its author wrote"}

# Elements whose text is never a token: a span inside one is not markup, is
# not drawn (an SVG drawing), or is not shown on the page; and a `<time>`,
# whose text is a moment as `moments.py` writes it.
PLAIN_TEXT = {"title", "option", "textarea", "script", "style", "svg", "time"}

_TAG = re.compile(r"<(/?)([A-Za-z][\w-]*)")

# A form's field is markup: Django renders a bound field as its widget and
# gives it `__html__`. A template read without its context cannot see a
# value's type, so it knows a field by what BoundField itself answers: a loop
# over a form or one of a form's field lists, a read of an attribute only a
# bound field has, or a template it includes that uses the name as one. A
# method of the field that returns markup (`as_hidden`, `label_tag`) prints
# markup. A name used as a field in one place is a field everywhere in that
# template. A form printed whole renders its fields, so a name read for what
# only a form answers (`non_field_errors`) is markup printed bare as well.
_FIELD_LOOP = re.compile(r"for\s+(\w+)\s+in\s+form(?:\.\w+)?")
_READ = re.compile(r"\b(\w+)\.(\w+)")
_INCLUDE = re.compile(r"""include\s+["']([^"']+)["'](?:\s+with\s+(.*?))?(\s+only)?$""")
_BINDING = re.compile(r"(\w+)=(\w+)(?=\s|$)")


@cache
def bound_field_reads() -> frozenset[str]:
    """The attributes a bound field answers and no string, dict, form field
    or model field does. A one-word name (`value`, `data`, `errors`) is as
    likely any object's, so only a compound one marks a field."""

    field = _ExampleForm()["name"]
    others = {*dir(str), *dir(dict), *dir(forms.Field), *dir(models.Field)}
    names = {*dir(type(field)), *vars(field)} - others
    return frozenset(name for name in names if not name.startswith("_") and "_" in name)


@cache
def form_reads() -> frozenset[str]:
    """The attributes a form answers and no string, dict, bound field, form
    field or model field does, compound as `bound_field_reads` keeps them."""

    form = _ExampleForm()
    others = {*dir(str), *dir(dict), *dir(form["name"]), *dir(forms.Field), *dir(models.Field)}
    names = {*dir(type(form)), *vars(form)} - others
    return frozenset(name for name in names if not name.startswith("_") and "_" in name)


@cache
def bound_field_renders() -> frozenset[str]:
    """The methods of a bound field that, called with nothing, return markup."""

    field = _ExampleForm()["name"]
    found = set()
    for name in bound_field_reads():
        member = getattr(field, name)
        if not callable(member):
            continue
        try:
            result = member()
        except TypeError:
            continue
        if hasattr(result, "__html__"):
            found.add(name)
    return frozenset(found)


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    value: str

    def __str__(self):
        return f"{self.path}:{self.line}: {{{{ {self.value} }}}}"


class _Markup:
    """Where a template's text stands as HTML: inside a tag, a comment, or an
    element whose text stays plain."""

    def __init__(self):
        self.in_tag = False
        self.quote = ""
        self.in_comment = False
        self.open: list[str] = []
        self._tag = ""

    def read(self, text: str) -> None:
        at = 0
        while at < len(text):
            at = self._step(text, at)

    def _step(self, text: str, at: int) -> int:
        if self.in_comment:
            end = text.find("-->", at)
            self.in_comment = end < 0
            return len(text) if end < 0 else end + 3
        if self.in_tag:
            return self._in_tag(text, at)
        if text.startswith("<!--", at):
            self.in_comment = True
            return at + 4
        if found := _TAG.match(text, at):
            self.in_tag = True
            self._tag = found.group(1) + found.group(2).lower()
            return found.end()
        return at + 1

    def _in_tag(self, text: str, at: int) -> int:
        char = text[at]
        if self.quote:
            self.quote = "" if char == self.quote else self.quote
        elif char in "\"'":
            self.quote = char
        elif char == ">":
            self.in_tag = False
            self._closed()
        return at + 1

    def _closed(self) -> None:
        name = self._tag.lstrip("/")
        if name not in PLAIN_TEXT:
            return
        if self._tag.startswith("/"):
            if name in self.open:
                self.open.remove(name)
        else:
            self.open.append(name)

    @property
    def in_text(self) -> bool:
        return not (self.in_tag or self.in_comment or self.open)


def filters(contents: str) -> list[str]:
    """The names of the filters a variable passes through, in order."""

    return [found["filter_name"] for found in filter_re.finditer(contents) if found["filter_name"]]


def last_filter(contents: str) -> str:
    """The name of the last filter a variable passes through, or ""."""

    names = filters(contents)
    return names[-1] if names else ""


def holds_a_literal(contents: str) -> bool:
    """Whether a printed variable hands a string written in the template to a
    hold. Django marks a template's literal safe, and every hold passes safe
    text through as markup, so the hold does nothing to it."""

    printed = contents.split("|")[0].strip()
    literal = printed[:1] in "\"'" or printed.startswith("_(")
    return literal and bool(HOLDING.intersection(filters(contents)))


def _included_fields(contents: str, included: Mapping[str, set[str]]) -> set[str]:
    """The names an include tag hands a template that uses them as fields:
    each bound with `with`, and the rest of the context unless `only`."""

    found = _INCLUDE.fullmatch(contents.strip())
    if not found:
        return set()
    theirs = included.get(found[1], set())
    bindings = dict(_BINDING.findall(found[2] or ""))
    names = {value for key, value in bindings.items() if key in theirs}
    if not found[3]:
        names |= theirs - bindings.keys()
    return names


def form_fields(source: str, included: Mapping[str, set[str]] | None = None) -> set[str]:
    """The names ``source`` gets as a form's fields or uses as one or as a
    form, with ``included`` naming the fields of each template it may include."""

    reads = bound_field_reads() | form_reads()
    names = set()
    for token in Lexer(source).tokenize():
        if token.token_type == TokenType.BLOCK and (loop := _FIELD_LOOP.fullmatch(token.contents.strip())):
            names.add(loop[1])
        if token.token_type == TokenType.BLOCK:
            names |= _included_fields(token.contents, included or {})
        if token.token_type in (TokenType.BLOCK, TokenType.VAR):
            names.update(found[1] for found in _READ.finditer(token.contents) if found[2] in reads)
    return names


def template_fields(sources: Mapping[str, str]) -> dict[str, set[str]]:
    """The field names of each template, by template name, through the
    templates it includes."""

    fields: dict[str, set[str]] = {name: set() for name in sources}
    changed = True
    while changed:
        changed = False
        for name, source in sources.items():
            found = form_fields(source, fields)
            if found != fields[name]:
                fields[name] = found
                changed = True
    return fields


def is_markup(contents: str, fields=frozenset()) -> bool:
    """Whether a printed variable is markup before any filter: a field of
    ``fields`` or what one renders, a block's parent, or a form's error list."""

    printed = contents.split("|")[0].strip()
    base, _, attribute = printed.rpartition(".")
    return (
        printed in MARKUP
        or printed in fields
        or (bool(base) and attribute in MARKUP_ATTRIBUTES)
        or (base in fields and attribute in bound_field_renders())
    )


def holds_markup(contents: str, fields=frozenset()) -> bool:
    """Whether a printed variable hands markup to a hold, which passes it as
    it is, so the hold only hides what the value is."""

    return is_markup(contents, fields) and bool(HOLDING.intersection(filters(contents)))


def holds(contents: str, fields=frozenset()) -> bool:
    """Whether a printed variable holds the names in it, or has none to hold."""

    names = filters(contents)
    while names and names[-1] in KEEPS_MARKUP:
        names.pop()
    if not names:
        return is_markup(contents, fields)
    return names[-1] in HOLDING or names[-1] in WORDS_ONLY or names[-1] in MARKUP_FILTERS


# What writes markup: printed where text belongs (an attribute, a title), its
# tags show as text or break the tag around them.
WRITES_MARKUP = HOLDING | {"linebreaksbr", "safe"}


@dataclass
class Reading:
    """What one template prints: the values it prints as text without holding
    their names, the words it writes itself and the markup it hands to a hold,
    markup it prints where text belongs, and the blocks it opens where text
    belongs, for a template that fills them."""

    unheld: list = field(default_factory=list)
    literals: list = field(default_factory=list)
    markup: list = field(default_factory=list)
    misplaced: list = field(default_factory=list)
    plain_blocks: set = field(default_factory=set)


def _block(contents: str) -> tuple[str, str]:
    words = contents.split()
    return (words[0], words[1] if len(words) > 1 else "") if words else ("", "")


def read(source: str, plain_blocks=frozenset(), included=None) -> Reading:
    """``source`` read as HTML, a block named in ``plain_blocks`` standing
    where text belongs, ``included`` naming each template's fields."""

    markup = _Markup()
    reading = Reading()
    fields = form_fields(source, included)
    commented = False
    blocks: list[str] = []
    for token in Lexer(source).tokenize():
        if token.token_type == TokenType.BLOCK:
            word, name = _block(token.contents)
            commented = word == "comment" or (commented and word != "endcomment")
            if word == "block":
                blocks.append(name)
                if not markup.in_text:
                    reading.plain_blocks.add(name)
            elif word == "endblock" and blocks:
                blocks.pop()
        elif commented:
            continue
        elif token.token_type == TokenType.TEXT:
            markup.read(token.contents)
        elif token.token_type == TokenType.VAR:
            in_text = markup.in_text and not plain_blocks.intersection(blocks)
            if in_text and holds_a_literal(token.contents):
                reading.literals.append(token)
            elif in_text and holds_markup(token.contents, fields):
                reading.markup.append(token)
            elif in_text and not holds(token.contents, fields):
                reading.unheld.append(token)
            elif not in_text and WRITES_MARKUP.intersection(filters(token.contents)):
                reading.misplaced.append(token)
    return reading


def unheld(source: str, path: str = "", plain_blocks=frozenset(), included=None) -> list[Site]:
    """Each value ``source`` prints as text without holding its names."""

    return [Site(path, token.lineno, token.contents) for token in read(source, plain_blocks, included).unheld]


def held_literals(source: str, path: str = "", plain_blocks=frozenset(), included=None) -> list[Site]:
    """Each word ``source`` writes itself and hands to a hold, which leaves it as it is."""

    return [Site(path, token.lineno, token.contents) for token in read(source, plain_blocks, included).literals]


def held_markup(source: str, path: str = "", plain_blocks=frozenset(), included=None) -> list[Site]:
    """Each value ``source`` knows is markup and hands to a hold."""

    return [Site(path, token.lineno, token.contents) for token in read(source, plain_blocks, included).markup]


def misplaced(source: str, path: str = "", plain_blocks=frozenset(), included=None) -> list[Site]:
    """Each value ``source`` prints as markup where text belongs."""

    return [Site(path, token.lineno, token.contents) for token in read(source, plain_blocks, included).misplaced]


# A value drawn in a box (a held name, an entity, a cell's line; a moment
# `when` prints is words in a `<time>`), then what
# renders nothing or closes around it (a closing tag, a block tag, a list's
# space between items), then a mark. A box as wide as its line leaves the mark
# no room after it, so the mark starts the next line alone: the hold takes it
# instead (`|breakable:"."`, `{% entity link mark="," %}`).
_BOXED = (
    r"\{\{[^{}]*\|(?:" + "|".join(sorted(HOLDING - {"when"})) + r")(?::(?:\"[^\"]*\"|\w+))?\s*\}\}"
    r"|\{%\s*(?:entity|cell_line)\b[^%]*%\}"
)
_SILENT = (
    r"(?:</[a-z]+>|\{%\s*(?:endif|endfor|endwith)\s*%\}|\{%\s*if\b[^%]*%\}"
    r"|\{%\s*if not forloop\.last\s*%\}[^{]*\{%\s*endif\s*%\})*"
)
_STRANDED = re.compile(rf"(?:{_BOXED}){_SILENT}[.,;:!?)\]]")
# Text only a screen reader hears has no line for a mark to start.
_UNSEEN = re.compile(r'<span class="visually-hidden">.*?</span>', re.DOTALL)


def stranded(source: str, path: str = "") -> list[str]:
    """Each mark ``source`` writes after a boxed value rather than through its hold."""

    seen = _UNSEEN.sub(lambda match: "\n" * match[0].count("\n"), source)
    return [f"{path}:{seen.count(chr(10), 0, match.start()) + 1}: {match[0]}" for match in _STRANDED.finditer(seen)]


def plain_blocks(sources) -> set[str]:
    """The blocks any of ``sources`` opens where text belongs (`{% block title %}` in a `<title>`)."""

    return set().union(*(read(source).plain_blocks for source in sources))


def template_paths() -> list[Path]:
    root = Path(settings.BASE_DIR) / "templates"
    return sorted(root.rglob("*.html"))


class UnheldTests(SimpleTestCase):
    """What the check reads as a value printed as text."""

    def values(self, source):
        return [site.value for site in unheld(source)]

    def test_a_bare_value_in_text_is_found(self):
        self.assertEqual(self.values("<small>{{ host.name }}</small>"), ["host.name"])

    def test_a_value_in_a_sentence_is_found(self):
        self.assertEqual(self.values("<p>Runs on {{ host }}.</p>"), ["host"])

    def test_a_held_value_passes(self):
        self.assertEqual(self.values("<small>{{ host.name|default:'-'|breakable }}</small>"), [])

    def test_a_filter_that_holds_names_itself_passes(self):
        self.assertEqual(self.values("<dd>{{ value|readout }} {{ at|when }}</dd>"), [])

    def test_a_literal_handed_to_a_hold_is_found(self):
        source = """<span>{{ "read-only"|breakable }} {{ 'example-word'|when }} {{ _("x-y")|readable }}</span>"""

        found = [site.value for site in held_literals(source)]

        self.assertEqual(found, ['"read-only"|breakable', "'example-word'|when", '_("x-y")|readable'])
        self.assertEqual(self.values(source), [])

    def test_a_literal_given_words_or_printed_where_text_is_plain_passes(self):
        source = """<span>{{ "serious"|urgency }}</span><title>{{ "x-y"|breakable }}</title>"""

        self.assertEqual([site.value for site in held_literals(source)], [])

    def test_a_count_passes(self):
        self.assertEqual(self.values('<span>{{ n|counted:"host" }}</span>'), [])

    def test_a_filter_before_the_hold_does_not_hold(self):
        self.assertEqual(self.values("<span>{{ host|breakable|upper }}</span>"), ["host|breakable|upper"])

    def test_an_attribute_value_passes(self):
        source = '<a href="{{ url }}" title="{{ host }}" {% if x %}data-x="{{ y }}"{% endif %}>x</a>'

        self.assertEqual(self.values(source), [])

    def test_plain_text_elements_pass(self):
        source = "<title>{{ a }}</title><select><option>{{ b }}</option></select><textarea>{{ c }}</textarea>"

        self.assertEqual(self.values(source), [])

    def test_text_after_a_plain_text_element_is_read_again(self):
        self.assertEqual(self.values("<title>{{ a }}</title><h1>{{ b }}</h1>"), ["b"])

    def test_a_value_held_before_a_line_break_passes(self):
        self.assertEqual(
            self.values("<p>{{ note|breakable|linebreaksbr }} {{ other|linebreaksbr }}</p>"), ["other|linebreaksbr"]
        )

    def test_markup_passes(self):
        source = (
            "{% for field in form %}<div>{{ field }} {{ form.non_field_errors }} {{ field.help_text|safe }}</div>"
            "{% endfor %}{% for hidden in form.hidden_fields %}{{ hidden }}{% endfor %}"
        )

        self.assertEqual(self.values(source), [])

    def test_a_name_used_as_a_field_is_markup(self):
        source = '<label for="{{ field.id_for_label }}">x</label>{{ field }}{{ field.errors }}'

        self.assertEqual(self.values(source), [])

    def test_a_name_that_is_not_a_field_is_text(self):
        source = "{% for field in publication.fields %}<code>{{ field }}</code>{% endfor %}"

        self.assertEqual(self.values(source), ["field"])

    def test_markup_handed_to_a_hold_is_found(self):
        source = (
            "{% for hidden in form.hidden_fields %}{{ hidden|breakable }}{% endfor %}"
            "{% block x %}{{ block.super|breakable }}{% endblock %}<p>{{ field.errors|breakable }}</p>"
        )

        found = [site.value for site in held_markup(source)]

        self.assertEqual(found, ["hidden|breakable", "block.super|breakable", "field.errors|breakable"])
        self.assertEqual(self.values(source), [])

    def test_a_value_named_like_a_field_but_not_one_may_be_held(self):
        source = "{% for field in node.unconfirmed_fields %}<code>{{ field|breakable }}</code>{% endfor %}"

        self.assertEqual(held_markup(source), [])

    def test_what_a_field_renders_is_markup_whatever_the_loop_names_it(self):
        source = "{% for item in material %}{{ item.label_tag }}{{ item }}{{ item.as_hidden }}{% endfor %}"

        self.assertEqual(self.values(source), [])

    def test_a_name_an_included_template_uses_as_a_field_is_markup(self):
        included = template_fields(
            {
                "partials/_form_field.html": '<label for="{{ field.id_for_label }}">x</label>{{ field }}',
                "partials/_other.html": '<label for="{{ field.id_for_label }}">x</label>{{ field }}',
            }
        )
        source = (
            '{% for field in material %}{% include "partials/_form_field.html" %}{{ field }}{% endfor %}'
            '{% for entry in identity %}{% include "partials/_form_field.html" with field=entry %}'
            "{{ entry }}{% endfor %}"
        )

        self.assertEqual(unheld(source, included=included), [])

    def test_a_form_printed_whole_is_markup(self):
        source = (
            "{% if material.non_field_errors %}{{ material.non_field_errors }}{% endif %}{{ material }}"
            "<p>{{ material.title|breakable }}</p>{{ identity }}"
        )

        self.assertEqual([site.value for site in unheld(source)], ["identity"])

    def test_an_include_that_hands_on_no_field_names_none(self):
        included = template_fields({"partials/_form_field.html": "{{ field.id_for_label }}"})
        source = (
            '{% for field in setup.fields %}{% include "partials/_form_field.html" only %}<code>{{ field }}</code>'
            '{% include "partials/_form_field.html" with field=other %}{% endfor %}'
        )

        self.assertEqual([site.value for site in unheld(source, included=included)], ["field"])

    def test_the_field_reads_are_what_a_bound_field_answers(self):
        field = _ExampleForm()["name"]

        for name in bound_field_reads():
            with self.subTest(name=name):
                self.assertTrue(hasattr(field, name))
                self.assertFalse(hasattr("", name) or hasattr({}, name))
        self.assertLessEqual({"id_for_label", "html_name"}, bound_field_reads())
        self.assertLessEqual({"as_hidden", "label_tag"}, bound_field_renders())

    def test_an_svg_drawing_passes(self):
        self.assertEqual(self.values("<svg><text>{{ mark.label }}</text></svg><p>{{ b }}</p>"), ["b"])

    def test_a_block_a_base_opens_in_a_title_passes(self):
        plain = plain_blocks(
            ["<title>{% block title %}{% endblock %} · HQ</title><h1>{% block head %}{% endblock %}</h1>"]
        )
        source = "{% block title %}{{ host }}{% endblock %}{% block head %}{{ host }}{% endblock %}"

        self.assertEqual([site.value for site in unheld(source, plain_blocks=plain)], ["host"])

    def test_markup_where_text_belongs_is_found(self):
        source = '<a title="{{ a|breakable }}">{{ b|breakable }}</a><title>{{ c|or_empty }}</title>'

        self.assertEqual([site.value for site in misplaced(source)], ["a|breakable", "c|or_empty"])

    def test_a_comment_passes(self):
        source = "<!-- {{ a }} -->{% comment %}<p>{{ b }}</p>{% endcomment %}{# {{ c }} #}"

        self.assertEqual(self.values(source), [])

    def test_a_greater_than_sign_inside_an_attribute_keeps_the_tag_open(self):
        self.assertEqual(self.values('<a data-x="a>b" title="{{ host }}">x</a>'), [])


class StrandedMarkTests(SimpleTestCase):
    """What the check reads as a mark written outside the box it follows."""

    found = staticmethod(stranded)

    def test_a_mark_after_a_held_value_is_found(self):
        self.assertEqual(len(self.found("<p>Runs on {{ host|breakable }}.</p>")), 1)

    def test_a_mark_after_a_closing_tag_or_a_branch_is_found(self):
        for source in (
            "<p>At <code>{{ host|breakable }}</code>, which</p>",
            "<p>{% entity link %}{% if x %}, {{ y }}{% endif %}</p>",
            "<p>{% for h in hosts %}{{ h|breakable }}{% if not forloop.last %} {% endif %}{% endfor %}.</p>",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(self.found(source)), 1)

    def test_a_mark_the_hold_takes_passes(self):
        source = (
            '<p>{{ host|breakable:"." }} {% entity link mark=forloop %}'
            '{% with mark=x|then:"," %}{{ y|breakable:mark }}{% endwith %} {{ z|breakable }} or</p>'
        )

        self.assertEqual(self.found(source), [])

    def test_a_mark_only_a_screen_reader_hears_passes(self):
        self.assertEqual(self.found('<a><span class="visually-hidden">{{ x|breakable }}: {{ y }}</span></a>'), [])

    def test_a_mark_in_the_other_branch_passes(self):
        self.assertEqual(self.found('{% if x %}<code>{{ x|breakable:"." }}</code>{% else %}.{% endif %}'), [])


class _ExampleForm(forms.Form):
    name = forms.CharField()
    key = forms.CharField(widget=forms.HiddenInput)


class HoldingFilterTests(SimpleTestCase):
    """The filters the check counts on hold any value they are given."""

    def test_a_mark_ends_the_last_words_innermost_box(self):
        printed = breakable("example-connection-with-a-long-name", ".")

        self.assertRegex(printed, r'name<span class="mark" aria-hidden="true">\.</span></span>(</span>)*$')

    def test_a_link_is_named_without_its_mark(self):
        link = EntityLink(kind_label="Service", label="example.com", url="/services/example.com/")

        printed = entity({}, link, mark=",")

        (hidden,) = [element for element in select(printed, "aria-hidden", "true") if "mark" in element.classes]

        self.assertEqual(hidden.text, ",")
        self.assertIn(hidden.ancestors[0], elements(printed, "a"))
        self.assertEqual(elements(printed, "a")[0].text, "example.com")

    def test_a_list_takes_a_comma_between_its_items(self):
        self.assertIn('aria-hidden="true">,<', breakable("example-host", {"last": False}))
        self.assertNotIn("mark", breakable("example-host", {"last": True}))

    def test_markup_passes_breakable_as_it_is(self):
        markup = format_html('<span class="pill pill-{}">{}</span>', "needs-review", "Needs review")

        self.assertEqual(breakable(markup), markup)

    def test_a_forms_fields_and_errors_are_markup(self):
        form = _ExampleForm()

        for value in (*form, *form.hidden_fields(), form["name"].errors, form.non_field_errors()):
            with self.subTest(value=type(value).__name__):
                self.assertTrue(hasattr(value, "__html__"))

    def test_text_that_names_no_moment_is_held(self):
        for printed in (when("example-docker-host-01"), readable("example-docker-host-01")):
            with self.subTest(printed=printed):
                self.assertIn("data-sole", printed)


@tag("source", "tree")
class TemplateValueTests(SimpleTestCase):
    def setUp(self):
        root = Path(settings.BASE_DIR)
        self.sources = {str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in template_paths()}
        self.plain = plain_blocks(self.sources.values())
        self.included = template_fields(
            {str(Path(path).relative_to("templates")): source for path, source in self.sources.items()}
        )

    def test_every_value_printed_as_text_holds_its_names(self):
        found = [
            site for path, source in self.sources.items() for site in unheld(source, path, self.plain, self.included)
        ]

        self.assertEqual([str(site) for site in found], [])

    def test_no_word_the_template_writes_is_handed_to_a_hold(self):
        found = [
            site
            for path, source in self.sources.items()
            for site in held_literals(source, path, self.plain, self.included)
        ]

        self.assertEqual(
            [str(site) for site in found],
            [],
            "Django marks a template's own string safe, and a hold passes safe text through as markup, so the hold"
            " does nothing: write the word held in the template, inside a `.whole` span as `|breakable` writes one",
        )

    def test_no_markup_is_handed_to_a_hold(self):
        found = [
            site
            for path, source in self.sources.items()
            for site in held_markup(source, path, self.plain, self.included)
        ]

        self.assertEqual(
            [str(site) for site in found],
            [],
            "A hold passes markup as it is, so it only hides that the value is markup: print it bare",
        )

    def test_no_markup_is_printed_where_text_belongs(self):
        found = [
            site for path, source in self.sources.items() for site in misplaced(source, path, self.plain, self.included)
        ]

        self.assertEqual([str(site) for site in found], [])

    def test_every_mark_after_a_boxed_value_is_written_by_its_hold(self):
        found = [site for path, source in self.sources.items() for site in stranded(source, path)]

        self.assertEqual(
            found,
            [],
            'A box as wide as its line leaves a mark after it no room: give the mark to the hold (`|breakable:"."`,'
            ' `|breakable:forloop` for a list\'s commas, `{% entity link mark="," %}`). A run of code chips takes'
            " no commas: their gaps separate them.",
        )


# What a `format_html` builder may print as text: a call that holds its names
# or composes markup whose own values are read where it was built, and an
# amount.
HOLDING_CALLS = HOLDING | {"format_html", "format_html_join", "mark_safe", "money"}
_PLACEHOLDER = re.compile(r"\{(\w*)\}")
# A format string that is attributes to add to a tag (' title="{}"') starts
# inside that tag.
_ATTRIBUTES = re.compile(r"\s+[\w:-]+=")


def _called(node: ast.AST) -> str:
    func = node.func if isinstance(node, ast.Call) else None
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else ""


def _text_placeholders(fmt: str) -> list[str]:
    """The placeholders of a format string printed as text, by position
    ("0", "1", ...) or by name."""

    markup = _Markup()
    markup.in_tag = bool(_ATTRIBUTES.match(fmt))
    found = []
    at = 0
    position = 0
    for match in _PLACEHOLDER.finditer(fmt):
        markup.read(fmt[at : match.start()])
        at = match.end()
        name = match[1] or str(position)
        position += not match[1]
        if markup.in_text:
            found.append(name)
    return found


def _locals(scope: ast.AST) -> dict[str, ast.AST]:
    return {
        target.id: node.value
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


class _Builders:
    """The `format_html` calls of one module, read against its own helpers."""

    def __init__(self, source: str, path: str):
        self.path = path
        self.tree = ast.parse(source)
        self.functions = [
            node for node in ast.walk(self.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        self.helpers = {function.name: function for function in self.functions}
        self._asked: set[str] = set()

    def held(self, node: ast.AST, assigned: dict[str, ast.AST]) -> bool:
        """Whether a builder's argument holds its names: a call that does, a
        helper of this module whose every result does, a choice's label, a
        literal, a constant HQ wrote, or a local set to one of these."""

        if isinstance(node, ast.IfExp):
            return self.held(node.body, assigned) and self.held(node.orelse, assigned)
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Name):
            return node.id.isupper() or (node.id in assigned and self.held(assigned[node.id], assigned))
        called = _called(node)
        if called in HOLDING_CALLS or (called.startswith("get_") and called.endswith("_display")):
            return True
        return called in self.helpers and self._helper_holds(called)

    def _helper_holds(self, name: str) -> bool:
        if name in self._asked:
            return False
        self._asked.add(name)
        helper = self.helpers[name]
        assigned = _locals(helper)
        returned = [node.value for node in ast.walk(helper) if isinstance(node, ast.Return) and node.value]
        return bool(returned) and all(self.held(value, assigned) for value in returned)

    def _unheld_in(self, call: ast.Call, assigned: dict[str, ast.AST]) -> list[Site]:
        if not (isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
            return []
        arguments = {str(index): arg for index, arg in enumerate(call.args[1:])}
        arguments |= {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg}
        found = []
        for name in _text_placeholders(call.args[0].value):
            argument = arguments.get(name)
            if argument is None or isinstance(argument, ast.Starred) or not self.held(argument, assigned):
                shown = ast.unparse(argument) if argument is not None else f"{{{name}}}"
                found.append(Site(self.path, call.lineno, shown))
        return found

    def unheld(self) -> list[Site]:
        found = []
        seen = set()
        # The innermost scope first, so a call reads the locals of its own function.
        for scope in [*reversed(self.functions), self.tree]:
            assigned = _locals(scope)
            for call in ast.walk(scope):
                if id(call) in seen or _called(call) != "format_html" or not call.args:
                    continue
                seen.add(id(call))
                self._asked.clear()
                found += self._unheld_in(call, assigned)
        return sorted(found, key=lambda site: site.line)


def unheld_builders(source: str, path: str = "") -> list[Site]:
    """Each value a `format_html` call in ``source`` prints as text without holding its names."""

    return _Builders(source, path).unheld()


def builder_paths() -> list[Path]:
    root = Path(settings.BASE_DIR)
    return sorted(
        path for package in ("hq", "hq_sdk") for path in (root / package).rglob("*.py") if "tests" not in path.parts
    )


class UnheldBuilderTests(SimpleTestCase):
    """What the check reads as a value a Python builder prints as text."""

    def values(self, source):
        return [site.value for site in unheld_builders(source)]

    def test_a_bare_value_in_text_is_found(self):
        self.assertEqual(self.values("format_html('<b>{}</b> · {}', host, n)"), ["host", "n"])

    def test_a_held_value_passes(self):
        self.assertEqual(self.values("format_html('<b>{}</b>', breakable(host))"), [])

    def test_an_attribute_value_passes(self):
        source = 'format_html(\'<a href="{}" title="{t}">{}</a>\', url, breakable(x), t=y)'

        self.assertEqual(self.values(source), [])

    def test_a_local_set_to_a_held_value_passes(self):
        source = (
            "def f():\n"
            "    label = breakable(x) if a else format_html('{}', breakable(y))\n"
            "    return format_html('<b>{}</b>', label)\n"
        )

        self.assertEqual(self.values(source), [])

    def test_a_value_spread_from_a_tuple_is_found(self):
        self.assertEqual(self.values("format_html('<a href=\"{}\">{}</a>', *mention)"), ["{1}"])

    def test_attributes_added_to_a_tag_pass(self):
        self.assertEqual(self.values("format_html(' title=\"{}\"', link.title)"), [])

    def test_a_helper_that_returns_held_markup_passes(self):
        source = (
            "def mention(link):\n"
            "    return format_html('<a href=\"{}\">{}</a>', link.url, breakable(link.label))\n"
            "def bare(link):\n"
            "    return link.label\n"
            "format_html('from {} and {}', mention(a), bare(b))\n"
        )

        self.assertEqual(self.values(source), ["bare(b)"])

    def test_words_hq_writes_pass(self):
        self.assertEqual(self.values("format_html('{} {} {}', MISSING, 'x', row.get_status_display())"), [])


@tag("source", "tree")
class BuilderValueTests(SimpleTestCase):
    def test_every_value_a_builder_prints_as_text_holds_its_names(self):
        root = Path(settings.BASE_DIR)
        found = []
        for path in builder_paths():
            found += unheld_builders(path.read_text(encoding="utf-8"), str(path.relative_to(root)))

        self.assertEqual([str(site) for site in found], [])
