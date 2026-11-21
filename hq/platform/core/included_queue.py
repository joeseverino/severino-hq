"""The shared parts a template includes by path that say what waits on the
operator: the queue, and the pieces a row of it draws on its open line, each
a table's cell can draw as a ``Piece``."""

from hq.platform.core.part_examples import records
from hq.platform.core.template_parts import included as _part

WORK_QUEUE = _part(
    "work_queue",
    "What waits on the operator, each row what wants doing and why, or that nothing does.",
    *("attention-clear", "is-mark"),
    pieces=("partials/_urgency_pill.html",),
    public=True,
    rules="""
    **A queue row says what is wrong and holds what fixes it.** The queue is named "Needs you"
    wherever it appears. Every list of problems is drawn by it, so every row's urgency, words and
    buttons share one edge. The queue page, the findings page, the dashboard's short queue, a thing's
    problems, the checks a subject does not meet, the steps a request contradicts and a domain's own
    attention list all use one projection (`hq/platform/application/decisions.py`). They draw it as
    one declared table (its rows `queue_table`). A page never draws a list of its own (the
    `list` shape of `hand_drawn.SHAPES`). A row is a table row as any other: its words in the cell that names
    it, what it offers in its actions (`Row(actions=)`), so it stands at the table's own density and gives way
    as every table does. It reads, in order:

    - its first line (`row-line`): the title and, only when it is urgent, the word "Urgent" after it
      in its tone (the words are `decisions.URGENCY`; a row that needs attention carries no mark, its
      word kept for a screen reader), then what it means (`row-gist`), quiet. The title states what is
      wrong and links to the thing it is about. It is what a reader presses, so it leads; a domain's
      heading, a family's name and their counts read below it. What it means is one line cut at its
      end, on the title's line where both fit, released in place by the row's toggle; a held
      request's says what is asked, the whole change its buttons decide one press away;
    - quiet under it, once out of date, how old the reading it is drawn from is
      (`Insight(observed_at=...)`). An out-of-date reading never asserts a live condition: its owner
      says what was read, in the past tense, as a notice. These are words under the title, not a
      column: most rows have none. Since when (`Insight(since=...)`) is said in the origin panel, never
      on the row;
    - buttons: the owner's actions, the first step that can be taken now when the owner offered none,
      and the check that reads again. At most one is primary. Where the reader sorts their own queue,
      Dismiss or Restore is the last of them (`row_actions put_away=`).

    Closed, a row is one line where its words fit. Everything taller is the row's `row-below`, opened
    by its toggle into the line beneath, from the title's edge to the table's. A row toggles only where
    opening it shows something more, or releases a cut meaning, and with no script every row shows all
    of it. The line beneath holds, in order:

    - what was read that shows it (`Insight(facts=...)`), each label once with its values beside it;
    - what to do, where a command or an instruction is a step, one standing alone and several
      numbered. A command is `{% command text=... %}`;
    - the reason it cannot be done from here, as a sentence and never as a step;
    - the pages that help look into it (`Insight(links=...)`), the map's as icons with HQ's
      tooltip;

    The survey holds "words swapped for a glyph".

    The word is `partials/_urgency_pill.html`. A Cmd- or Ctrl-click on it opens what raised it, read
    from the code rather than written down. That is the finding rule, its severity and the line its
    detector is defined at, or the domain and key of anything else.

    A next step an owner names as a short label with a `url` is a link to that page; one it writes as
    a sentence is shown as a sentence. Each action renders once, by method and URL, through the
    page-action renderer and the shared POST form, and the transport keeps the owner's full workflow.
    A dismissed row is its title and the way back. A link to a row (`/action-items/#…`) lands on it
    open.

    Several items of one family (`Insight(family=...)`) fold under a row saying how bad, what kind
    and how many, which opens to their rows: on the queue page (`queue_table`) and
    wherever a thing's problems are drawn (`fold`). The findings page keeps each row its own, its
    filter being by rule.

    A queue of three or fewer is read on the dashboard, under the greeting, as the queue's own rows
    (`glance`), under no heading of their own. A longer one is a count and a link to its page, except
    the held requests, which are read there first at any length. There a row is its title, what it
    means and its buttons, holding nothing beneath; its title opens the row on the page that shows the
    rest, steps included, or a held request's own page.

    **An item is open until its owner stops raising it.** Nobody closes one by looking at it. What a
    reader can do with one they are not acting on is dismiss it. It leaves their queue and their
    counts, stays reachable in a fold below while its owner still raises it, and comes back on its own
    when it gets worse, grows, or is raised again after clearing (its `since` after the dismissal).
    The header and the dashboard count what waits; a dashboard with only dismissed items says so and
    links to them, and never claims all is clear. A held request (`Insight(held=True)`) is decided by
    its Approve or Reject, never dismissed: its row has no Dismiss and no mark hides it
    (`action_items.sortable`). It is composed with no decision on it: each reader is offered only the
    decisions the rule that refuses them lets that reader take (`approvals.offers`), and a decision
    withheld says why among the row's facts.

    **A family folds.** An owner names what kind of matter an item is (`Insight(family="Image
    advisories")`), in the words it would head a list with. Several open items of one family from one
    domain are one line in the queue (how bad, what kind, how many), a row that opens to theirs, and
    can be dismissed whole. A family of one is the item alone.

    **Dismissing a row costs a write, not a queue.** A row's button names the row itself (its revision
    and key), so dismissing or restoring one composes nothing. With script it happens in place, the
    row becoming one quiet line with the way back and the counts over it following. Only "all of
    these" asks the server what "these" are.

    **A notice is not something to do.** An owner marks an item a notice (`Insight(notice=True)`) when
    it reports something that happened or was measured and there is nothing to resolve. The queue
    keeps notices in their own section, out of the "needs you" count, and a dismissed notice is done
    with: it returns only when what it reports changes. The line is drawn once
    (`action_items.split_waiting`) from what each item says of itself.

    The queue page is sections by the domain that raised each item, derived from what the items carry
    (`hq/platform/application/action_items.py`), so a domain that starts raising items has its section
    without being listed anywhere. Filters that match nothing offer a way back; they never imply a
    healthy estate.
    """,
    example=records.WORK_QUEUE,
    states=records.WORK_QUEUE_STATES,
)
ORIGIN_PANEL = _part(
    "origin_panel",
    "What raised a queue item, in the popover its urgency opens: why, what raised it, and how old its reading is.",
    example=records.ORIGIN_PANEL,
    rules="""
    Given the item's decision (`application.decisions`), it is drawn after the line its urgency
    stands on, never inside its words, and a Cmd- or Ctrl-click on the urgency opens it (app.js).
    """,
)
INSIGHT_FACTS = _part(
    "insight_facts",
    "What was read that shows a problem, `facts`: each label once, with every value it was given.",
    example=records.INSIGHT_FACTS,
)
OPERATOR_STEPS = _part(
    "operator_steps",
    "What an operator does about a problem, `steps`: numbered, each a command or words with its notes.",
    example=records.OPERATOR_STEPS,
)
ICON_LINKS = _part(
    "icon_links",
    "The pages that help look into a problem, `links`: one HQ maps to an icon drawn as it, its label the tip.",
    example=records.ICON_LINKS,
)
