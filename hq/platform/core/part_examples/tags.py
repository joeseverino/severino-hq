"""The examples of the parts a tag draws, beyond the facts and the section."""

lazy import json
lazy from dataclasses import replace
lazy from datetime import UTC, datetime, timedelta
lazy from uuid import UUID

lazy from django import forms
lazy from django.contrib.auth import get_user_model
lazy from django.test import RequestFactory

from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example
lazy from hq.domains.control_plane.models import ApprovalRequest
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.approval_review import SUMMARY, Asked, ChangePreview, compare, grouped
lazy from hq.platform.application.approvals import DECISIONS, NO_OFFER, Offer, _decision_link
lazy from hq.platform.application.capability_policy import RULE_LABELS, Column, Group, PolicyCell, Row
lazy from hq.platform.application.contracts import Effect
lazy from hq.platform.application.credential_mint import CredentialFix
lazy from hq.platform.application.finding_steps import Run
lazy from hq.platform.application.money import money
lazy from hq.platform.application.pages import PageAction, RowMark, RowOrder
lazy from hq.platform.application.payload_words import ChangeRow
lazy from hq.platform.application.security import MCP_INTERFACE
lazy from hq.platform.application.tables import Cell, DataTable, TableColumn
lazy from hq.platform.application.work_runs import RunEvidence, outcome_tone
lazy from hq.platform.core.models import AgentIdentity

# The fields of an example record, as a change sets them and as two records hold them.
_CHANGED_AFTER = {
    "example_effect": "infrastructure_change",
    "example_timeout_seconds": 300,
    "example_hosts": ["example-host-01", "example-host-02"],
}


_RECORDS = {
    "example.first": {"example_effect": "read", "example_timeout_seconds": 60},
    "example.second": {"example_effect": "read", "example_timeout_seconds": 120, "example_retry": "safe"},
}


def _list_insert() -> tuple[dict[str, str], dict[str, str]]:
    """A policy document before and after one grant is inserted among six and one test is widened."""

    grants = [{"src": [f"group:example-{index}"], "dst": ["tag:example"], "ip": ["tcp:443"]} for index in range(6)]
    tests = [{"src": f"user-{index}@example.com", "accept": ["tag:example:443"]} for index in range(2)]
    inserted = {"src": ["group:example-admins"], "dst": ["tag:example"], "ip": ["*"]}
    after = {
        "grants": [*grants[:3], inserted, *grants[3:]],
        "tests": [tests[0], {**tests[1], "accept": ["tag:example:443", "tag:example:22"]}],
    }
    return (
        {"document": json.dumps({"grants": grants, "tests": tests})},
        {"document": json.dumps(after)},
    )


_LIST_INSERT = _list_insert()


def _policy():
    """Agents' rules as the Agents page draws them for one agent: a row per
    action, its rule in every state a rule can be in, and a cell past the
    agent's grant. The catalog draws without script, which drops no column,
    so one agent's column is the table that fits every width."""

    options = (("", "Default"), *((rule, words) for rule, words in RULE_LABELS.items()))
    column = Column("example-agent", "example-agent", "MCP", AgentIdentity(client_id="example-agent"))

    def row(name, label, effect, default, rule, unavailable=""):
        cell = PolicyCell(
            f"example-agent-{name}",
            column.label,
            rule,
            options,
            unavailable=unavailable,
            label=f"{label}, for {column.label}",
        )
        return Row(name, label, effect, default, (cell,))

    return {
        "columns": (column,),
        "label": "Example rules",
        "groups": (
            Group(
                "Example records",
                (
                    row("example.edit", "Edit an example record", Effect.REMOTE_WRITE, "On", "allow"),
                    row("example.move", "Move an example record", Effect.REMOTE_WRITE, "On", None),
                    row(
                        "example.stop",
                        "Stop an example container",
                        Effect.DESTRUCTIVE,
                        "Ask me first",
                        "approve",
                    ),
                    row(
                        "example.delete",
                        "Delete an example record",
                        Effect.DESTRUCTIVE,
                        "Ask me first",
                        "deny",
                    ),
                ),
            ),
            Group(
                "Example hosts",
                (row("example.read", "Read an example host", Effect.READ, "On", None, "Not granted"),),
            ),
        ),
    }


def _approval(state, **decided):
    """An approval card's entry (approvals.review()) for a request an agent
    asked with its reason, in ``state``, with what its decision said."""

    held = ApprovalRequest(
        id=UUID("00000000-0000-4000-8000-000000000001"),
        state=state,
        requested_interface=MCP_INTERFACE,
        reason="Adopt the example playbooks so the example-host-01 upgrade can run in tonight's window.",
        created_at=DRAWN_AT - timedelta(minutes=12),
        expires_at=DRAWN_AT + timedelta(hours=23),
        **decided,
    )
    pending = state == ApprovalRequest.State.PENDING
    return {
        "entry": {
            "held": held,
            "tone": "attention" if pending else "",
            "asked": Asked("example-agent", "wants to adopt the playbooks", "", ""),
            "preview": compare({}, _CHANGED_AFTER, label="Would change"),
            "warnings": ("The check playbook can read changed files every playbook can read.",) if pending else (),
            "resource_url": "/example/",
            "resource_label": "Playbooks",
            "resource_open": "Open Playbooks",
            # As an operator is offered it: both decisions while it waits.
            "offer": Offer(tuple(_decision_link(held.id, decision) for decision in DECISIONS), "")
            if pending
            else NO_OFFER,
        }
    }


def _arranged_rows():
    """Services as the services table arranges them: three favorites, in the
    order kept, and one that is not."""

    names = ("app.example.com", "example-dashboard-with-a-long-name.example.com", "home.example.com")
    favorites = [
        (name, RowMark("/example/pin/", True, name, "favorites"), RowOrder("/example/move/", name, at > 0, at < 2))
        for at, name in enumerate(names)
    ]
    return [*favorites, ("shop.example.com", RowMark("/example/pin/", False, "shop.example.com", "favorites"), None)]


class _ExampleDistance(forms.Form):
    distance_miles = forms.DecimalField(label="Example miles", min_value=0)


# A table whose rows each open an editor under them (`row_editor`).
def _edited_rows(name, first, second):
    """A table named ``name`` whose rows ``first`` and ``second`` each open an editor."""

    return (
        f'{{% section "{name}" %}}{{% table_region "{name}" %}}<table class="data-table"><thead><tr>'
        '{% table_heading "Run" "identifier" %}{% table_heading "Miles" "numeric" %}'
        '{% table_heading "Distance check" "control" %}</tr></thead><tbody>'
        f'<tr><td>{first}</td><td>3.10</td><td>{{% row_editor form "{first}" label="Correct distance" '
        'action="/example/distance/" verb="Save distance" distance_miles="3.10" %}</td></tr>'
        f'<tr><td>{second}</td><td>5.00</td><td>{{% row_editor form "{second}" label="Correct distance" '
        'action="/example/distance/" verb="Save distance" distance_miles="5.00" %}</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}"
    )


def _event(task: str, outcome: str, *, host: str = "example-01", message: str = "") -> dict:
    """A task's outcome as a run reports it (the bridge's ``RunEvent``), with its tone."""

    return {"host": host, "task": task, "outcome": outcome, "message": message, "tone": outcome_tone(outcome)}


_LONG_HOST = "example-machine-with-a-long-name-01.example.com"


def _example_runs():
    return (
        (
            "Run example.clear_cache",
            "Running",
            JobState.RUNNING.tone,
            "Applying the example playbook.",
            RunEvidence(
                hosts=("example-01", "example-02"),
                correlation="example-investigation-7",
                worker="example-automation-worker",
                lease_until=datetime(2026, 3, 2, 9, 35, tzinfo=UTC),
                events=(
                    _event("Flush the example cache", "changed"),
                    _event("Check the example cache", "ok", host="example-02"),
                ),
            ),
        ),
        ("Refresh playbooks", "Queued", JobState.QUEUED.tone, "Waiting for HQ to start it.", RunEvidence()),
        (
            "Run example.upgrade_packages",
            "Failed",
            JobState.FAILED.tone,
            "The example upgrade failed on one machine.",
            RunEvidence(
                hosts=(_LONG_HOST,),
                correlation="example-investigation-with-a-correlation-id-longer-than-a-phone-is-wide-0123456789",
                events=(
                    _event(
                        "Upgrade every example package to the release the example repository pins",
                        "failed",
                        host=_LONG_HOST,
                        message="example-package-with-a-long-name 2.0.1 conflicts with "
                        "example-other-package-with-a-long-name 1.4.0, which the example machine also holds.",
                    ),
                ),
            ),
        ),
        (
            "Run example.report_disks",
            "Done",
            JobState.SUCCEEDED.tone,
            "Reported every example disk.",
            RunEvidence(
                hosts=("example-01",),
                stated=("example.filesystem", "example.reading_with_a_longer_name"),
                events=tuple(
                    _event(f"Example task {number}", outcome)
                    for number, outcome in enumerate(("ok", "changed", "skipped", "unreachable") * 3, start=1)
                ),
            ),
        ),
    )


# What each part's default entry, its Part.example, is given (catalog.default_name).
# A viewer signed in with no role, for a part that asks what its viewer may see.
def _signed_in_request():
    return RequestFactory().get("/example/")


def _signed_in_request():
    """A viewer signed in with no role, for a part that asks what its viewer may see."""

    request = RequestFactory().get("/example/")
    request.user = get_user_model()(username="example-owner")
    return request


MUTED_STATES = {
    # Muted words as a line beside a value and as a paragraph under it.
    "beside-a-value": Example(
        '{% section "Example muted words" %}<p>3 example checks {% muted %}since the last read{% endmuted %}</p>'
        '{% muted element="p" %}An example sentence under the value, long enough to wrap at a phone width.'
        "{% endmuted %}{% endsection %}"
    ),
}


# A choice of one ({% select %}) with a visible label, then one named by
# its label alone in each status tone, as a rule's setting is.
SELECT = Example(
    '{% control_label "Example rule" control="example-rule" %}'
    '{% select "example" options chosen="allow" id="example-rule" %}'
    '{% for tone in tones %} {% select "example-"|add:tone options chosen=tone label="Example rule, "|add:tone '
    "tone=tone %}{% endfor %}",
    context={
        "options": (
            ("", "Default"),
            ("allow", "On"),
            ("good", "Example good"),
            ("attention", "Example attention"),
            ("serious", "Example serious"),
            ("neutral", "Example neutral"),
        ),
        "tones": ("good", "attention", "serious", "neutral"),
    },
)


SELECT_STATES = {
    # Agents' rules (core/_policy_table.html), each setting a toned select
    # (core/_policy_setting.html): every state a rule can be in, and a cell
    # past an agent's grant.
    "policy-table": Example('{% include "core/_policy_table.html" %}', _policy),
}


STEPS_STATES = {
    # Steps done in order, each numbered: said in words, and a command to run
    # with its notes, as a credential's fix draws one.
    "numbered": Example(
        '{% section "Example upgrade" %}{% steps numbered=True %}'
        '{% step %}<strong>Pull the example image</strong> <span class="muted">An example sentence that says what the step does.</span>{% endstep %}'
        '{% include "partials/_operator_step.html" %}'
        "{% step %}<strong>Check the example</strong>{% endstep %}"
        "{% endsteps %}{% endsection %}",
        lambda: {
            "step": Run(
                "Make a new example token on your Mac",
                "example-mint --account example-01 --item example-token --permission example.read",
                notes=("HQ could not tell which example account holds it.", "Then press Refresh."),
            )
        },
    ),
    # A connection whose credential lacks permissions: its headline in the
    # row, its fix opened beneath it.
    "credential-fix": Example(
        '{% section "Example connections" %}{% table_region "Example connections" %}<table class="data-table">'
        '<thead><tr>{% table_heading "Name" "identifier" %}{% table_heading "Provider" %}</tr></thead>'
        '<tbody><tr><td>example-main{% include "control_plane/_credential_fix.html" %}</td><td>Example</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        lambda: {
            "fix": CredentialFix(
                connection_ref="example-main",
                provider="example",
                missing=("example.read", "example.write"),
                command="example-mint --account example-01 --item example-token",
                by_hand="the example provider's console",
            )
        },
    ),
    # Steps and checks side by side: met, unmet, done and plain.
    "states": Example(
        '{% section "Example standard" %}{% steps %}'
        '{% step icon="check-circle" tone="good" title="An example check holds." %}<strong>Example met</strong> <span class="muted">Stops an example.</span>{% endstep %}'
        '{% step icon="shield" tone="attention" %}<strong>Example unmet</strong> <span class="muted">An example is open: <a href="/example/">close it.</a> Stops an example.</span>{% endstep %}'
        '{% step icon="check-circle" done=True %}<strong>Example done</strong> <span class="muted">Done.</span>{% endstep %}'
        '{% step icon="card" %}<strong>Example step</strong> <span class="muted">An example sentence that says what the step is for.</span>{% endstep %}'
        "{% endsteps %}{% endsection %}"
    ),
}


BUTTON = Example(
    '{% button restart.label offer=restart look="primary" %}',
    context=lambda: {"restart": PageAction._built(label="Restart", url="/example/restart/", method="post")},
)


BUTTON_STATES = {
    # A row's buttons whose labels end in a name, one of them hyphenated, as a
    # queue row draws a plan for each container behind: the hyphenated part is
    # a box of its own (`breakable`), set on the line of the words before it.
    "in-rows": Example(
        '{% table_region "Example plans" %}<table class="data-table"><tbody>{% for action in actions %}<tr><td>'
        '{% include "partials/_action_links.html" with actions=action only %}</td></tr>{% endfor %}</tbody></table>'
        "{% endtable_region %}",
        lambda: {
            "actions": (
                (PageAction("Upgrade plan for example", "/example/"),),
                (PageAction("Upgrade plan for example-app", "/example/app/"),),
            )
        },
    ),
}


# A run's evidence ({% run_evidence %}) in its row's open line, as Jobs,
# a machine's page and a procedure's page draw it: a run with every field,
# one with none, one whose names and messages run long, and one with many
# tasks that stated readings.
RUN_EVIDENCE = Example(
    '{% section "Example runs" %}{% table_region "Example runs" %}<table class="data-table"><thead><tr>'
    '{% table_heading "Playbook" %}{% table_heading "State" "status" %}'
    '{% table_heading "Result" %}</tr></thead><tbody>'
    "{% for title, word, tone, note, evidence in runs %}<tr><td><strong>{{ title|breakable }}</strong></td>"
    "<td>{% status word tone=tone %}</td><td>{{ note }}{% run_evidence evidence %}</td></tr>{% endfor %}"
    "</tbody></table>{% endtable_region %}{% endsection %}",
    context=lambda: {"runs": _example_runs()},
)


CARD_STATES = {
    # A change's before and after ({% change_table %}), each state its two
    # callers draw: an approval card's preview and an audit event's changes.
    # An empty one draws nothing beside what its section already says.
    # A held request (partials/approval_card.html): one waiting, its reason
    # among its facts and its decision's two buttons, and one decided, with
    # the note its decider wrote.
    "approval-pending": Example(
        '{% include "partials/approval_card.html" %}', lambda: _approval(ApprovalRequest.State.PENDING)
    ),
    "approval-decided": Example(
        '{% include "partials/approval_card.html" %}',
        lambda: _approval(
            ApprovalRequest.State.REJECTED,
            decided_actor="example-person",
            decided_at=DRAWN_AT - timedelta(minutes=3),
            decision_note="The upgrade waits for the example-host-02 backup to finish; ask again tomorrow.",
        ),
    ),
    # A card that is the whole of a page outside the frame, as the sign-in
    # draws one: its mark, its own title, its one action and its closing line.
    "alone": Example(
        '{% load static %}{% card element="div" alone=True label="Example sign-in" %}'
        '<img src="{% static "img/logo.png" %}" alt="Example" width="64" height="64">'
        "<h1>Example sign-in</h1>"
        '<p class="muted">An example line before the action.</p>'
        '<p><a class="btn primary" href="/example/">Use example sign-on</a></p>'
        '<p><small class="muted">An example closing line.</small></p>{% endcard %}',
        heads_the_page=True,
    ),
}


# One field that moves, its values long enough to wrap on a phone, and
# one whose words never reach the log.
CHANGE_TABLE = Example(
    "{% change_table preview %}",
    context=lambda: {
        "preview": ChangePreview(
            label="Changes",
            rows=(
                *compare(
                    {"example_description": "An example description long enough to wrap on a phone's width."},
                    {"example_description": "Another example description, longer, so it wraps across lines too."},
                    label="Changes",
                ).rows,
                ChangeRow(label="Example token", before="", after="", change="changed", redacted=True),
            ),
            lines=(),
        )
    },
)


CHANGE_TABLE_STATES = {
    "empty": Example(
        '{% section "Example request 3" %}<p>An example start, which changes no field.</p>'
        "{% change_table preview %}{% endsection %}",
        lambda: {"preview": compare({}, {}, label="Would change")},
    ),
    # What is written where nothing stood, such as a first adoption: one column.
    "first": Example(
        "{% change_table preview %}",
        lambda: {"preview": compare({}, _CHANGED_AFTER, label="Would be applied")},
    ),
    # Many records of one kind, as a catalog's playbooks: what belongs to
    # none, one that moves, one that is new (said by its summary, its fields
    # folded), one that goes, one that stays and so is only named under the
    # table, and a document that would not parse, said as a line diff. Each
    # kind of change has its own status.
    "many": Example(
        "{% change_table preview %}",
        lambda: {
            "preview": replace(
                grouped(
                    {
                        "commit": {"revision": "0123456789abcdef" * 2 + "01234567"},
                        "records": {**_RECORDS, "example.retired": {"example_effect": "read"}},
                    },
                    {
                        "commit": {"revision": "fedcba9876543210" * 2 + "fedcba98"},
                        "records": {
                            **_RECORDS,
                            "example.first": {**_RECORDS["example.first"], **_CHANGED_AFTER},
                            "example.third": {
                                SUMMARY: {
                                    "label": "An example record",
                                    "value": "Changes an example thing\u00a0· Any example machine\u00a0· No sudo",
                                },
                                **_CHANGED_AFTER,
                            },
                        },
                    },
                    label="Would change",
                ),
                lines=tuple(
                    compare(
                        {"example_notes": "# An example note\nfirst line\nsecond line"},
                        {"example_notes": "# An example note\nfirst line, changed\nsecond line"},
                        label="",
                    ).lines
                ),
            )
        },
    ),
    # A document whose list gains one entry mid-way and has another edited:
    # the new entry is one row, the edited one its field, and the entries
    # after the insert stay unsaid.
    "list": Example(
        "{% change_table preview %}",
        lambda: {"preview": compare(*_LIST_INSERT, label="Would change", amends=True)},
    ),
}


CONTROL_LABEL_STATES = {
    # A control label ({% control_label %}) with no control, naming the answer
    # that follows it; the filter bar above draws it for its controls
    # (CatalogTests in test_part_tags).
    "states": Example(
        '{% card element="article" label="Example stage" %}{% control_label "Example receiving" %}'
        "<p><strong>Example provider</strong></p>{% endcard %}"
    ),
}


COMMAND_STATES = {
    # Text to paste somewhere else: a command short and long enough to wrap
    # at its slashes and spaces, then a block of code as a container's compose
    # change draws it, a title naming a long path, a line longer than a
    # phone's, the words under it and its notes.
    "states": Example(
        '{% section "Example setup" %}{% command text="example-cli status" %}{% command text=long %}'
        '{% command id="example-patch" %}{{ yaml|breakable }}'
        "{% title %}Add to <code>{{ compose_file|breakable }}</code>"
        "{% detail %}Where its service already has one of these keys, replace it. Then run "
        "<code>docker compose up -d {{ service|breakable }}</code> in that directory."
        "{% notes %}<li><strong>Remove</strong> <code>pid: host</code></li>"
        '<li><code>user</code> <span class="muted">Its files must be readable by 1000:1000.</span></li>'
        '<li class="prose"><strong>Not included</strong> <span class="muted">An example reason it leaves out,'
        ' with <a href="/example/">an example link</a>.</span></li>'
        "{% endcommand %}{% endsection %}",
        {
            "long": "example-cli connect --config /etc/example/example-agent/config.toml "
            "--token-file /run/secrets/example-agent-token --name example-host-01.example.com",
            "yaml": "services:\n"
            "  example-service-with-a-long-name:\n"
            "    volumes:\n"
            '      - "/opt/example-applications/an-example-application-with-a-long-name/configuration:/srv/configuration:ro"\n'
            '    user: "1000:1000"',
            "compose_file": "/opt/example-applications/an-example-application/compose.production.yaml",
            "service": "example-service-with-a-long-name",
        },
    ),
}


CHECK_LADDER_STATES = {
    # A route as a check ladder: a stop that holds, opened to its own checks
    # (one read from a value, one that does not hold with the rules it rests
    # on), a stop not read, and one that does not hold, drawn opened.
    "route": Example(
        '{% section "Example path 4" %}{% check_ladder route=True %}'
        '{% check_rung state="met" kind="Device" open=True data_example="device" %}example-laptop{% detail %}Example OS'
        '{% more %}2 checks{% page %}<a href="/example/machines/example-laptop/">example-laptop</a>{% body %}<p>Example HQ confirmed this example device.</p>'
        "{% check_ladder %}"
        '{% check_rung state="met" evidence="192.0.2.10" %}Example address is the device\'s'
        "{% body %}<p>The address the request came from is the device's own.</p>{% endcheck_rung %}"
        '{% check_rung state="unmet" evidence=long %}Example policy admits the device'
        "{% body %}<p>No example rule admits it.</p>{% list %}<li><code>example:rule-one</code></li>"
        "<li><code>example:rule-two</code></li>{% endlist %}{% endcheck_rung %}"
        "{% endcheck_ladder %}{% endcheck_rung %}"
        '{% check_rung kind="Tailnet" %}example-tailnet.example.com{% more %}Details'
        "{% body %}<p>Example HQ could not read the tailnet.</p>{% endcheck_rung %}"
        '{% check_rung state="unmet" kind="HQ" open=True %}hq.example.com{% more %}Details'
        "{% body %}<p>This request disagrees with what example HQ read here.</p>"
        '{% notice tone="warning" %}Point example-hq.example.com at the example proxy.{% endnotice %}{% endcheck_rung %}'
        "{% endcheck_ladder %}{% endsection %}",
        {"long": "example-tag:example-device-with-a-long-name.example.com"},
    ),
    # A route with no stop open, so it runs across its box: a stop that holds,
    # stops no check reads, each drawn as what it is, and one not read.
    "route-across": Example(
        '{% section "Example path 5" %}{% check_ladder route=True %}'
        '{% check_rung state="met" kind="Device" %}example-laptop{% more %}Details'
        "{% body %}<p>Example HQ confirmed this example device.</p>{% endcheck_rung %}"
        '{% check_rung icon="proxy" kind="Proxy" %}example-proxy{% more %}Details'
        "{% body %}<p>An example proxy no check reads.</p>{% endcheck_rung %}"
        '{% check_rung icon="container" kind="Container" %}example-app{% detail %}Up 3 days{% more %}Details'
        "{% body %}<p>An example container no check reads.</p>{% endcheck_rung %}"
        '{% check_rung kind="Served" %}example.example.com{% more %}Details'
        "{% body %}<p>The example controller has not read it.</p>{% endcheck_rung %}"
        "{% endcheck_ladder %}{% endsection %}",
    ),
    # A stop no check reads, opened: its mark untoned, the route drawn down.
    "route-unchecked": Example(
        '{% section "Example path 6" %}{% check_ladder route=True %}'
        '{% check_rung state="met" kind="Device" %}example-laptop{% more %}Details'
        "{% body %}<p>Example HQ confirmed this example device.</p>{% endcheck_rung %}"
        '{% check_rung icon="server" kind="Machine" open=True %}example-host-01{% more %}Details'
        '{% page %}<a href="/example/machines/example-host-01/">example-host-01</a>'
        "{% body %}<p>An example machine no check reads.</p>{% endcheck_rung %}"
        "{% endcheck_ladder %}{% endsection %}",
    ),
    # Checks read down as rows, with no route: each in a frame of its own.
    "states": Example(
        '{% section "Example checks 2" %}{% check_ladder %}'
        '{% check_rung state="met" evidence="192.0.2.10" %}Example address is known'
        "{% body %}<p>Example HQ knows this address.</p>{% endcheck_rung %}"
        '{% check_rung state="unmet" %}Example sign-in is linked{% body %}<p>No example account links it.</p>'
        "{% endcheck_rung %}"
        "{% check_rung %}Example lock is read{% body %}<p>Example HQ could not read it.</p>{% endcheck_rung %}"
        "{% endcheck_ladder %}{% endsection %}",
    ),
}


ROW_EDITOR = Example(
    '{% row_editor form row.pk label="Edit" action=row.edit_url verb="Save" %}',
    context=lambda: {
        "form": ROW_EDITOR_STATES["states"].given()["form"],
        "row": {"pk": 1, "edit_url": "/example/1/"},
    },
    address="/example/",
)


ROW_EDITOR_STATES = {
    # A row's editor: the way in, closed, and the address that opens the
    # first row's form in the line beneath it.
    "states": Example(
        _edited_rows("Example runs to correct", "example-01", "example-02"),
        lambda: {"form": _ExampleDistance()},
        address="/example/runs/",
    ),
    "open": Example(
        _edited_rows("Example runs corrected", "example-11", "example-12"),
        lambda: {"form": _ExampleDistance()},
        address="/example/runs/?edit=example-11",
    ),
}


# A row's actions: one that posts a value, and one that deletes.
ROW_ACTIONS = Example(
    '{% table_region "Example actions" %}<table class="data-table"><tbody><tr><td>{{ row.name }}</td>'
    "{% row_actions row.actions %}</tr></tbody></table>{% endtable_region %}",
    lambda: {
        "row": {
            "name": "example-01",
            "actions": (
                PageAction._built(label="Read", url="/example/1/status/", method="post", value="read", field="status"),
                PageAction._built(label="Delete", url="/example/1/delete/", danger=True),
            ),
        }
    },
)
ROW_ACTIONS_STATES = {
    # A row's own arrangement in its controls column: favorites with the pair
    # that moves them (the first with no way up, the last with no way down),
    # and a row that is not one, which draws its star alone.
    "arrangement": Example(
        '{% section "Example services" %}{% table_region "Example services" %}<table class="data-table"><thead>'
        '<tr>{% table_heading "Service" "identifier" %}{% table_heading "Open to" %}'
        '{% table_heading "Actions" css="actions-col" hidden=True %}</tr></thead>'
        "<tbody>{% for name, mark, order in rows %}<tr><td><strong>{{ name|breakable }}</strong></td>"
        "<td>Example network</td>{% row_actions mark=mark order=order %}</tr>{% endfor %}"
        "</tbody></table>{% endtable_region %}{% endsection %}",
        lambda: {"rows": _arranged_rows()},
    ),
}


DATA_TABLE = Example(
    "{% data_table table %}",
    context=lambda: {
        "table": DataTable.of(
            "Example hosts",
            (
                TableColumn("Host", cell=lambda name: Cell(name, f"/example/{name}/")),
                TableColumn("State", cell=lambda name: Cell("Online", tone="good")),
                TableColumn("Cost", kind="numeric", cell=lambda name: Cell(money(len(name)))),
            ),
            ("example-host-01", "example-host-two"),
        )
    },
)


DOT = Example("{{ count }}{% dot %}{{ age }}", context={"count": "3 example hosts", "age": "2 days ago"})


ENTITY = Example('{% entity "machine"|entity_of:name %}', context={"name": "example-host-01"})


PRESENT = Example(
    "{% present repo.release %}<code>{{ repo.release }}</code>{% endpresent %}", context={"repo": {"release": "v1.4.2"}}
)


REFERENCE = Example(
    '{% reference expense "paid_from" as account %}{% entity account %}',
    context=lambda: {"request": _signed_in_request()},
)


REFERENCED_BY = Example(
    '{% referenced_by "machine" machine.name %}',
    context=lambda: {"machine": {"name": "example-host-01"}, "request": _signed_in_request()},
)


PAGE_MAIN = Example("{% page_main %}<h1>Example page</h1><p>An example line.</p>{% endpage_main %}", frame="main")


ROW_TOGGLE = Example(
    '{% table_region "Example toggles" %}<table class="data-table"><tbody><tr>'
    "<td>{{ row.name }}{% row_toggle %}</td></tr></tbody></table>{% endtable_region %}",
    context={"row": {"name": "example-host-01"}},
)
