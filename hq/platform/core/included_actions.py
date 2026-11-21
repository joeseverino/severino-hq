"""The shared parts a template includes by path that offer what a reader
can do: an ask, a page's action, the gap where a change cannot be offered,
and a held request's card with its decisions."""

from hq.platform.core.part_examples import pages, tags
from hq.platform.core.template_parts import included as _part

ASK = _part(
    "ask",
    "A do-this-now control and how its work stands, from `hq_sdk.outbound.ask`.",
    *("ask", "ask-elapsed", "ask-note", "ask-said", "ask-sentence", "ask-why", "ask-word"),
    public=True,
    rules="""
    **Asked-for work is an ask.** A button that starts work which outlives the request is this part,
    from an `Ask` (`hq/platform/application/asks.py`). It stands where a `PageAction` does, in a page
    or panel head. In a body or panel its note takes a line of its own under the row its button stands
    in, reserved before anything is said. In a page's head it takes no room: the head's reading line
    ("Last read …", `partials/_reading_age.html`) says it, over the words the server drew there, which
    keep their room. Under way that is "Refreshing…" for any work; a failure's sentence is cut to the
    line, whole in its title. The note is then heard and not seen, and once the work lands the head is
    drawn again, its line saying the new reading's age. A head with no reading line says it on the
    busy button alone.

    Pressing it posts to a view that declares only what it asks (`asks.AskMixin`): the request runs
    the work's capability, stores the ask and answers at once, a refusal as failed work in the
    refusal's own sentence, and the control then says how the work stands. Every capability an ask
    runs answers `asks.Asked` (its standing, where to follow it, and what asking did), so the view
    reads the answer and words nothing of its own.

    - The note beside the button is a live region (`role="status"`) that changes only when the state
      does. The elapsed time ticks next to it outside the region, so nothing is read out every second.
    - While its own work is live the button is `aria-disabled`, not disabled: it keeps the focus the
      operator just gave it and ignores a second press.
    - When the work ends, the part of the page named by the ask's `refresh` selector is fetched again
      and swapped in place; without one, the page's content (`#content`, a region of its own). Only if
      that cannot be had does the page load again, though never while the operator is using it and
      never twice running; then it offers the reading as a link.
    - A failure is said in the note, where it was asked.

    In a table's row the note's line is as wide as the button, so there it says the state's word alone
    ("Waiting", "Failed") and the row keeps its height. The sentence is the button's title, and the
    line the row opens to says the word and the sentence under the button's label, so a reader with no
    title to hover still reads why.

    Each thing is said once. A sentence that only says the state's word again ("Waiting" and
    "Waiting…") is dropped where every standing is made (`asks.Standing`), so the word stands alone,
    in a cell and beside a head's button alike, and the row opens no line for it. A sentence earns its
    place by saying what the word cannot: what the work waits on, or why it failed.

    Without script the same button is a form post back to the page, which draws the control from what
    is stored. So a page loaded while the work is live says so and resumes following it.

    A job's panel (`_job_progress.html`) and the readings a page asks for when it opens
    (`_visit_refresh.html`) are the same ask drawn differently; one behavior in `ask.js` follows all
    three. *Gates:* `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behavior`;
    `lint:ast-grep` (`ask-answered-by-ask-mixin.yml`);
    `ComponentLayoutTests.test_a_rows_ask_says_its_sentence_on_the_line_its_row_opens_to`;
    `hq/platform/core/tests/test_action_budgets.py`.
    """,
    example=pages.ASK,
    states=pages.ASK_STATES,
)
PAGE_ACTION = _part(
    "page_action",
    "One control of a page or a row, from a `PageAction`, drawn by `{% button %}`; an ask, from an `Ask`.",
    pieces=("partials/_action_links.html",),
    rules="""
    A page's action is `{% include "partials/_page_action.html" with action=... %}`, and a run of an
    item's actions (`ActionLink`s, or their serialized dicts) is `partials/_action_links.html` with
    `actions=`, which leaves out a way that leads only to the view it is drawn on (`remedies`), so a box
    around them asks it first and is drawn only around a control. `extra` adds looks: a head draws a
    control twice, beside its title and in its menu, and both are the same control. `heard` marks the
    copy beside the title, whose ask is heard there and seen on the head's reading line.

    A disabled one, by any method, is never a link or a form: it stays in the tab order
    (`aria-disabled`) so its reason, the title, is read where it stands. One that names the view it is
    drawn on (a switch between views) is that view's state, `aria-current`, never a link to itself,
    unless it opens a dialog: then it is the dialog's one way in. A POST one is the button's `offer`,
    which sends what it carries.
    """,
    example=tags.PAGE_ACTION,
    states=tags.PAGE_ACTION_STATES,
)
WRITE_GAP = _part(
    "write_gap",
    "What stands where a change would be offered when its connection cannot make it, from a `WriteGap`.",
    rules="""
    Every page that would offer a change its connection cannot make offers this instead: what the change
    needs, linked to the setup step that gives it, with the whole reason as its title (`data-write-gap`).
    A row draws it through `{% row_actions gap=... %}` and a form through its `gap`.
    """,
    example=tags.WRITE_GAP,
    states=tags.WRITE_GAP_STATES,
)
APPROVAL_CARD = _part(
    "approval_card",
    "A held request, from `approvals.review()`: what was asked, its facts, its change and its decisions.",
    rules="""
    Its heading is what was asked, the aside when and how, and what it acts on, with the edge of its
    tone while it waits. A warning no row of the change carries stands above the change. Its buttons
    are the decisions its reader may take (`entry.offer`), and a decision withheld from them says why.
    """,
    example=tags.APPROVAL_CARD,
    states=tags.APPROVAL_CARD_STATES,
)
