"""Where a value's text may wrap onto the next line, and where it never may.

``breakable`` and ``code_spans`` are value_tags' filters, registered there; the
seams and boxes they draw are this module's, read by the stylesheet's
``.seam``, ``.token``, ``.whole``, ``.part``, ``.joined`` and ``.mark``.
"""

import re
from html import unescape

from django.utils.html import conditional_escape
from django.utils.safestring import mark_safe

from hq.platform.application.labels import DOT, DOT_MARKUP
from hq.platform.application.printable import admitted
from hq.platform.core.held_values import format_html

# Where an address, a hostname, a path or an image reference may wrap: after a
# slash, after a colon run (never inside a scheme's "://" or an IPv6 "::"), and
# after a dot unless digits stand either side of it, so a version ("2.39.8")
# and an IPv4 address are each one piece. An address's query wraps after its
# "?" and each "&" (escaped by then), so a word of it stays whole.
_BREAK_AFTER = re.compile(r"(/|:+|\?|&amp;|(?<!\d)\.|\.(?!\d))(?=[^/:])")
# A seam as markup: an empty span whose stylesheet text is a zero-width space
# (base.css). A `<wbr>` would do in WebKit, but Chromium breaks at one even
# where `white-space: nowrap` holds its line, and a zero-width space in the
# text itself would be copied with the name.
_SEAM = '<span class="seam"></span>'


# A part with a hyphen inside it ("nginx-proxy-manager") is kept on one line:
# a browser breaks after a hyphen, and a name split there reads as two. A flag
# ("-a") has nothing before its hyphen to break from.
_INNER_HYPHEN = re.compile(r"[^-\s]-")
# A name joined by an underscore ("infrastructure_change") is an identifier,
# however short, with no place a browser breaks it: held as a hyphenated part
# is, a cell cuts it rather than widen its column to its length.
_INNER_UNDERSCORE = re.compile(r"[^\W_]_+[^\W_]")
# A run longer than any word of a sentence (a digest, a key, a subject) has no
# seam to wrap at, so it is held on one line as a hyphenated part is: a cell
# cuts it at its edge rather than widening its column to fit it.
_UNBROKEN = 24
# A seam inside a word, between two of its letters or digits: an address, a
# path, a host name. A dot after a bracket ("(.zip)") is punctuation.
_INNER_SEAM = re.compile(r"\w(?:/|:+|\.)\w")
_WORDS = re.compile(r"(\s+)")
# A figure in a phrase: an amount, a time, an address of digits ("$12.00",
# "9:29", "172.18.0.2"), a range of two ("10.5–12.5"), a rate ("10:36/mi"), or
# a share or a prefix length ("3/4", "10.0.0.0/24").
# Nothing in one is a place a line breaks.
_FIGURE = re.compile(r"\W*\d+(?:[.:,]\d+)*(?:–\d+(?:[.:,]\d+)*)?(?:/(?:[^\W\d_]+|\d+(?:[.:,]\d+)*))?\W*")
# A quantity, which reads as one figure even as a whole value: a signed
# amount or count, a percent, a share ("5/5") or a rate ("9:59/mi"). An
# address of digits or a version is a name standing alone, so it is not one.
_QUANTITY = re.compile(r"[+−-]?\u2060?\$?\d+(?:,\d{3})*(?:\.\d+)?(?:%|/\d+)?|\d+(?::\d{2})+/[^\W\d_]+")
# Where a browser may break a hyphenated part: after each inner hyphen.
_AFTER_HYPHEN = re.compile(r"(?<=[^-\s]-)")
# A piece of a name shorter than this, alone on a line ("1" under
# "portainer-agent-"), reads as a stray mark rather than part of the name.
_SCRAP = 3


def _glued(pieces: list[str]) -> list[list[str]]:
    """``pieces`` grouped so that no group is a scrap: a short piece goes with
    the one before it, and a short first piece with the one after."""

    groups: list[list[str]] = []
    for piece in pieces:
        if groups and (len(unescape(piece)) < _SCRAP or len(unescape("".join(groups[-1]))) < _SCRAP):
            groups[-1].append(piece)
        else:
            groups.append([piece])
    return groups


# A label of a name, with the seam after it: letters, digits, hyphens. A
# bracket or a quote ("com']") makes the run a value that holds names, not one.
_LABEL = re.compile(r"[\w-]+(?:\.|/|:+)?")


def _labels_held(pieces: list[str]) -> list[str]:
    """``pieces`` with each dotted name's end labels held to their neighbors:
    the last label stays with the one before it ("example.com"), and the
    first with the next while the name still has a seam left to break at. A
    label alone on a line ("com") reads as a word rather than part of a name.
    A hyphenated first label already reads as a name, so it keeps its seam:
    held to the next label, the pair could break only at a hyphen.
    Applied to a sole token, a name that is its value whole."""

    out: list[str] = []
    start = 0
    while start < len(pieces):
        end = start
        while end < len(pieces) - 1 and pieces[end].endswith("."):
            end += 1
        run = pieces[start : end + 1]
        if all(_LABEL.fullmatch(piece) for piece in run) and len(run) > 1:
            run = [*run[:-2], run[-2] + run[-1]]
        if len(run) > 2 and not _INNER_HYPHEN.search(run[0]):
            run = [run[0] + run[1], *run[2:]]
        out.extend(run)
        start = end + 1
    return out


def _joined(text: str) -> str:
    return f'<span class="joined">{text}</span>'


def _held(group: list[str], *, first: bool, last: bool) -> str:
    """A group at an end of its part, held together. The hold's edges sit
    before a hyphen, where no line may break, never after one: a browser
    decides a break at an element's edge by the box around both sides, and
    where the part is text with no box of its own (a name cut in a cell),
    that box would let the line break there."""

    text = "".join(group)
    if len(group) == 1:
        return text
    if first and last:
        return _joined(text)
    if first:
        return _joined(text[:-1]) + "-"
    return group[0][:-1] + _joined("-" + "".join(group[1:]))


def _kept(part: str) -> bool:
    """Whether a part is held on one line: a hyphen or an underscore inside
    it, or a run too long to be a word."""

    return bool(_INNER_HYPHEN.search(part) or _INNER_UNDERSCORE.search(part)) or len(unescape(part)) > _UNBROKEN


def _whole(part: str, *, boxed: bool = False) -> str:
    """A part, kept on one line when it is ``_kept``. Where it must break
    after a hyphen all the same, a scrap at either end stays with its
    neighbor; one inside it has words on both sides to share its line.

    ``boxed`` puts its text in a box of its own (``.part``), which a table
    cell cuts at the cell's edge while the part around it asks for its whole
    length and holds out for none of it (figures.css)."""

    if not _kept(part):
        return part
    groups = _glued(_AFTER_HYPHEN.split(part))
    last = len(groups) - 1
    held = "".join(
        _held(group, first=at == 0, last=at == last) if at in (0, last) else "".join(group)
        for at, group in enumerate(groups)
    )
    inner = f'<span class="part">{held}</span>' if boxed else held
    return f'<span class="whole">{inner}</span>'


def _token(word: str, *, sole: bool) -> str:
    """A word with a seam or a hyphen inside it, or too long to be a word, as
    a token. Any other word reads as a word, and stays plain text. A token
    that is the whole value (a name, never a word of a sentence) is marked
    ``data-sole``. A figure among words reads as one of them."""

    if not (_INNER_SEAM.search(word) or _kept(word)) or (sole and _QUANTITY.fullmatch(unescape(word))):
        return word
    if not sole and _figure(word):
        # A browser breaks after a dash, so a range is a part of its own:
        # it moves to the next line whole and breaks at its dash only when it
        # alone is longer than its line.
        return f'<span class="whole">{word}</span>' if "–" in word else word
    # A word has no whitespace, so a newline marks where it splits.
    seams = _BREAK_AFTER.sub("\\1\n", word).split("\n")
    # Only a name standing alone holds its end labels: a value of several
    # words (a list, a sentence) shares its line, and a longer piece there
    # widens the column it sits in.
    seams = _labels_held(seams) if sole else seams
    parts = ["".join(group) for group in _glued(seams)]
    if not sole:
        # A word of a phrase: each part is cut on its own, at its cell's edge.
        return f'<span class="token">{_SEAM.join(_whole(part, boxed=True) for part in parts)}</span>'
    inside = _SEAM.join(_whole(part) for part in parts)
    return f'<span class="token" data-sole><span class="cut">{inside}</span></span>'


# The tags that close a word's boxes, after its last letter.
_CLOSING = re.compile(r"(?:</span>)*$")


def button_words(value):
    """A control's words as ``breakable`` draws them, in one box when they
    are drawn in more than one piece: a control is a flex box (page.css,
    Buttons), where each run of text and each token would be an item of its
    own, set apart by its gap and not by the space between the words."""

    words = breakable(value)
    if "<" not in words:
        return words
    return format_html('<span class="button-words">{}</span>', words)


def breakable(value, mark=""):
    """``value`` as tokens, one per word: escaped, with a line-break opportunity where an address or a path may wrap.

    The one way a template writes an identifier. ``mark`` is the punctuation
    after it, a list's ``forloop`` for its comma, or ``DOT_MARK`` for the dot
    ``{% dot %}`` draws.
    """

    mark = mark_text(mark)
    value = admitted(value)
    # Markup is whatever renders itself: a form field is not SafeData until
    # Django renders it. It passes as it is: what built it held the names in
    # it, and a seam inside a tag would break the tag.
    if hasattr(value, "__html__"):
        after = mark_safe(DOT_MARKUP) if mark == DOT_MARK else conditional_escape(mark)  # noqa: S308 - HQ's own literal markup
        return conditional_escape(value) + after
    if mark == DOT_MARK:
        return mark_safe("".join([*_inside_last(_line(value), DOT_MARKUP.rstrip()), " "]))  # noqa: S308 - each word is escaped as it is drawn
    return mark_safe("".join(_marked(_line(value), mark)))  # noqa: S308 - each word is escaped as it is drawn


# The mark that ends a value with ``{% dot %}``'s dot (``{{ x|breakable:"·" }}``),
# drawn inside the value's last box as ``labels.dotted`` parts are: a box is a
# place a line may break even before a no-break space (WebKit, as CSS Text
# says), so a dot written after it could open the next line.
DOT_MARK = "·"


def _marked(drawn: list[str], mark: str) -> list[str]:
    """Drawn words with ``mark`` inside the last word's innermost box: a box as
    wide as its line leaves a mark after it no room there, and the mark would
    start the next line alone."""

    if not mark:
        return drawn
    # Hidden from the accessibility tree: in a link it would be heard as part
    # of the link's name. ``data-mark`` is what a link draws in its place,
    # clear of its underline (base.css).
    shown = conditional_escape(mark)
    return _inside_last(drawn, f'<span class="mark" data-mark="{shown}" aria-hidden="true">{shown}</span>')


def _inside_last(drawn: list[str], markup: str) -> list[str]:
    """``markup`` written after the last word's last letter, inside its
    innermost box."""

    last = drawn[-1]
    at = _CLOSING.search(last).start()
    return [*drawn[:-1], f"{last[:at]}{markup}{last[at:]}"]


# A word a value ends on that a name before it holds: one word of letters,
# perhaps with its sentence's mark ("TXT", "pulled").
_TAIL = re.compile(r"\w*[^\W\d_]\w*[.,;:!?]?")


def _line(value, *, sole: bool | None = None) -> list[str]:
    """``value``'s words as ``breakable`` draws them, held so that no line they
    wrap onto starts with a stray: a dot ``labels.dotted`` joined parts with
    stays on the line of the part before it, and a word a value ends on
    after a name stays on the name's last line. Each is held inside the box before it, since a box as wide as
    its line leaves what follows no room there; a word a name holds stays
    outside the parts of it a cell cuts."""

    parts = str(value).split(DOT)
    alone = (len(parts) == 1 and not _WORDS.search(parts[0])) if sole is None else sole
    drawn: list[str] = []
    for at, part in enumerate(parts):
        words = _words(part, sole=alone)
        if len(words) >= 3 and 'class="token"' in words[-3] and _TAIL.fullmatch(words[-1]) and words[-2] == " ":
            # In the token's own box, after the parts a cell cuts; in a
            # phrase whose last part is a box of its own (`.whole`), inside
            # that box, after the text a cell cuts in it (`.part`).
            name = words[-3]
            boxed = "data-sole" not in name and 'class="part"' in name and name.endswith("</span>" * 3)
            closing = "</span>" * (2 if boxed else 1)
            name = name.removesuffix(closing)
            words = [*words[:-3], f"{name}\xa0{words[-1]}{closing}"]
        if at < len(parts) - 1:
            # The dot as {% dot %} draws it, hidden from a reader, and the
            # space after it outside the box, where the line may break.
            words = [*_inside_last(words, DOT_MARKUP.rstrip()), " "] if part else [DOT_MARKUP]
        drawn.extend(words)
    return drawn


# What a figure counts or measures, which the line never leaves alone below
# it ("12" above "days"): a word of letters, perhaps hyphenated ("sign-ins")
# or per another ("m/s"), or a percent sign, with the sentence's mark after it.
_UNIT = re.compile(r"(?:[^\W\d_]+(?:-[^\W\d_]+)*(?:/[^\W\d_]+)?|%)[.,;:!?)]*")
# A word of marks alone: a separator standing between two words ("+0.01 m / 90
# days", "Spec › Hosts") or a wildcard ("ip *"), which a line never opens on or
# holds alone, as the survey reads a stray.
_MARKS = re.compile(r"[\W_]+")


def _figure(word: str) -> bool:
    """Whether a word of a phrase reads as one figure (``_FIGURE``)."""

    return len(unescape(word)) <= _UNBROKEN and bool(_FIGURE.fullmatch(unescape(word)))


def _held_back(words: list[str], at: int) -> bool:
    """Whether the word at ``at`` stays on the line of the word before it: the
    unit or counted word after a figure, as typesetting sets them, or a word
    of marks alone."""

    word = unescape(words[at])
    return bool(_MARKS.fullmatch(word) or (_figure(words[at - 2]) and _UNIT.fullmatch(word)))


def _held_places(words: list[str]) -> list[int]:
    """Where in ``words`` (split by ``_WORDS``, so every second piece is the
    whitespace between two) a word stays on the line of the one before it.
    A line the text itself breaks (a block of code) is never joined back."""

    return [at for at in range(2, len(words), 2) if "\n" not in words[at - 1] and _held_back(words, at)]


def held(text: str) -> str:
    """``text`` as plain text, each figure joined to its unit or counted word by
    a no-break space as ``breakable`` joins them, and no markup added: for a
    value tag whose words go in an attribute as well as in a page."""

    words = _WORDS.split(str(text))
    for at in _held_places(words):
        words[at - 1] = "\xa0"
    return "".join(words)


def _words(value, *, sole: bool | None = None) -> list[str]:
    """``value``'s words as ``breakable`` draws them, a word standing alone as a
    name unless ``sole`` says otherwise."""

    # Escaped first, so the only markup in the result is what is added here.
    words = _WORDS.split(str(conditional_escape(value)))
    held = _held_places(words)
    # A token that fits on its line moves to the next one whole; one longer than
    # its line wraps at its seams; a part longer than its line alone breaks
    # wherever it must.
    alone = len(words) == 1 if sole is None else sole
    drawn = [_token(word, sole=alone) for word in words]
    # A word held back joins the one before it with a no-break space, inside
    # that word's innermost box (a range's): a box's edge is a place the line
    # breaks whatever space follows it. From the end, so each join leaves the
    # places of the ones before it.
    # A separator after a name's token stays outside it, clear of the parts
    # of it a cell cuts.
    for at in (at for at in reversed(held) if 'class="token"' not in drawn[at - 2]):
        drawn[at - 2 : at + 1] = _inside_last(drawn[at - 2 : at - 1], f"\xa0{drawn[at]}")
    return drawn


# A Markdown code span: the text between a pair of backticks.
_CODE_SPAN = re.compile(r"`([^`]+)`")
# The punctuation a sentence puts straight after a code span, which
# ``code_spans`` holds to the span as ``breakable`` holds its mark.
_TRAILING_MARK = re.compile(r"[.,;:!?)\]]+")


def code_spans(value):
    """``value``, prose written as Markdown, as ``breakable`` draws it, with each code span drawn as code.

    Each piece is escaped first, so the only markup is the ``<code>`` added
    here; the rest of Markdown is shown as written, since one card is no reason
    for a Markdown parser.
    """

    value = admitted(value)
    if hasattr(value, "__html__"):
        return conditional_escape(value)
    drawn = []
    # Split at a group, so every second piece is a span's text.
    pieces = _CODE_SPAN.split(str(value))
    for index, piece in enumerate(pieces):
        if not index % 2:
            # A word of a sentence, never a name standing alone.
            drawn.append("".join(_line(piece, sole=False)))
            continue
        # The punctuation after the span goes inside it, so it never starts a
        # line alone; the span's words are a sentence's too.
        after = _TRAILING_MARK.match(pieces[index + 1])
        mark = after.group() if after else ""
        pieces[index + 1] = pieces[index + 1][len(mark) :]
        drawn.append(f"<code>{''.join(_marked(_words(piece, sole=False), mark))}</code>")
    return mark_safe("".join(drawn).replace(DOT, DOT_MARKUP))  # noqa: S308 - each word is escaped as it is drawn


def mark_text(mark) -> str:
    """A mark as ``breakable`` takes it: the text given, or for a list's
    ``forloop`` a comma after every item but the last."""

    if isinstance(mark, dict):
        return "" if mark["last"] else ","
    return str(mark)
