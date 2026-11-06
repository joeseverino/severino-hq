"""What the Agents page says each agent did lately: read by actor from the
audit log over a window, and no dearer for a longer log or more agents."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from hq.platform.application import agent_activity, agent_limits, agent_registry, denials, roles
from hq.platform.application.agent_limits import RATE_LIMITED
from hq.platform.application.linked_accounts import bind_sign_in
from hq.platform.application.security import AuthorizationError, agent_principal
from hq.platform.core.models import AgentIdentity, AuditLog
from hq.platform.core.test_runner import no_default_role

AGENT = "example-agent"
OTHER = "example-other"


def _seen(client_id: str, granted=("read",), person=None) -> None:
    AgentIdentity.objects.create(client_id=client_id, interfaces=["mcp"], granted=list(granted), person=person)


def _person(username: str, role: str = ""):
    """A person signed in as ``username``'s key, holding ``role`` (none when blank)."""

    with no_default_role():
        user = get_user_model().objects.create_user(username)
    bind_sign_in(user, username)
    if role:
        roles.assign(user, role)
    return user


def _did(client_id: str, message: str = "Changed a record", *, days_ago: float = 0) -> AuditLog:
    return AuditLog.objects.create(
        action=AuditLog.Action.UPDATED,
        message=message,
        metadata={"actor": client_id},
        created_at=timezone.now() - timedelta(days=days_ago),
    )


class ActivityTests(TestCase):
    def test_changes_and_refusals_are_each_agents_own(self):
        _seen(AGENT)
        _seen(OTHER)
        _did(AGENT)
        _did(AGENT)
        denials.record_denial(interface="mcp", reason="forbidden", actor=AGENT)
        _did(OTHER)

        found = agent_activity.recent_activity([AGENT, OTHER])

        self.assertEqual((found[AGENT].changes, found[AGENT].refused), (2, 1))
        self.assertEqual((found[OTHER].changes, found[OTHER].refused), (1, 0))

    def test_a_counted_rate_refusal_stands_for_every_call_it_counted(self):
        _seen(AGENT)
        denials.record_denial(interface="mcp", reason=RATE_LIMITED, actor=AGENT)
        refused = AuditLog.objects.get(metadata__reason=RATE_LIMITED)
        refused.metadata = {**refused.metadata, "count": 14}
        refused.save(update_fields=["metadata"])
        denials.record_denial(interface="mcp", reason="forbidden", actor=AGENT)

        activity = agent_activity.recent_activity([AGENT])[AGENT]

        self.assertEqual((activity.refused, activity.limited), (15, 14))

    def test_only_the_window_counts_and_only_the_latest_are_shown(self):
        _seen(AGENT)
        _did(AGENT, "Long ago", days_ago=agent_activity.RECENT.days + 1)
        for minute in range(agent_activity.SHOWN + 2):
            _did(AGENT, f"Change {minute}", days_ago=minute / 1440)

        activity = agent_activity.recent_activity([AGENT])[AGENT]

        self.assertEqual(activity.changes, agent_activity.SHOWN + 2)
        self.assertEqual(len(activity.latest), agent_activity.SHOWN)
        self.assertEqual(activity.latest[0].message, "Change 0")
        self.assertNotIn("Long ago", [event.message for event in activity.latest])

    def test_hq_seeing_an_agent_is_not_something_it_did(self):
        agent_registry.observe(agent_principal(AGENT, "mcp", frozenset({"read"})))

        self.assertTrue(AuditLog.objects.filter(metadata__actor=AGENT).exists())
        self.assertEqual(agent_activity.recent_activity([AGENT]), {})

    def test_an_agent_with_nothing_lately_has_no_activity(self):
        _seen(AGENT)
        _did(AGENT, days_ago=agent_activity.RECENT.days + 1)

        self.assertEqual(agent_activity.recent_activity([AGENT]), {})

    def test_a_machines_whole_grant_is_in_force(self):
        _seen(AGENT, granted=("read", "write_calendar", "read_audit_log"))

        (standing,) = agent_activity.standings()

        self.assertEqual((standing.in_force, standing.beyond_role), (("read", "read_audit_log", "write_calendar"), ()))

    def test_a_machine_holds_none_of_a_persons_own_settings_in_force(self):
        # Written out: what keeps something of a person's, which a machine's token is refused.
        personal = ("set_preferences", "pin_favorites", "set_aside_action_items")
        _seen(AGENT, granted=("read", *personal))

        (standing,) = agent_activity.standings()

        self.assertEqual(standing.in_force, ("read",))


class PersonRoleTests(TestCase):
    """An agent a person signed in holds its grant within that person's role."""

    GRANTED = ("read", "write_calendar")

    def test_a_viewers_agent_holds_only_the_reads_and_the_page_says_whose_role(self):
        _seen(AGENT, granted=self.GRANTED, person=_person("kim", roles.Role.VIEWER))

        (standing,) = agent_activity.standings()

        self.assertEqual((standing.in_force, standing.beyond_role), (("read",), ("write_calendar",)))
        self.assertEqual(standing.role, roles.Role.VIEWER)
        self.assertIsNotNone(standing.role_confirmed)

    def test_the_page_shows_what_the_agents_principal_holds(self):
        for role in roles.Role:
            with self.subTest(role):
                AgentIdentity.objects.all().delete()
                _seen(AGENT, granted=self.GRANTED, person=_person(f"person-{role}", role))
                principal = agent_principal(AGENT, "mcp", frozenset(self.GRANTED), person_key=f"person-{role}")

                (standing,) = agent_activity.standings()

                self.assertEqual(set(standing.in_force), set(principal.capabilities))

    def test_what_the_page_says_is_missing_is_what_a_refusal_tells_its_person_to_grant(self):
        for role in roles.Role:
            with self.subTest(role):
                AgentIdentity.objects.all().delete()
                _seen(AGENT, granted=self.GRANTED, person=_person(f"missing-{role}", role))
                principal = agent_principal(AGENT, "mcp", frozenset(self.GRANTED), person_key=f"missing-{role}")

                (standing,) = agent_activity.standings()

                self.assertEqual(set(standing.missing), set(principal.grantable))

    def test_a_person_without_a_role_has_an_agent_refused_and_shown_with_nothing_in_force(self):
        _seen(AGENT, granted=self.GRANTED, person=_person("nobody"))

        (standing,) = agent_activity.standings()

        self.assertEqual(standing.in_force, ())
        self.assertTrue(standing.acts_for_nobody)
        with self.assertRaises(AuthorizationError):
            agent_principal(AGENT, "mcp", frozenset(self.GRANTED), person_key="nobody")

    def test_a_machines_own_token_acts_for_nobody_and_holds_its_grant(self):
        _seen(AGENT, granted=self.GRANTED)

        (standing,) = agent_activity.standings()

        self.assertEqual((standing.in_force, standing.role), (self.GRANTED, ""))
        self.assertFalse(standing.acts_for_nobody)


def _listening(client_id: str, streams: int) -> None:
    """``client_id`` holding ``streams`` listen streams open, as the MCP door holds each."""

    for _ in range(streams):
        held = agent_limits.LIMITS.listen(client_id)
        assert isinstance(held, agent_limits.Slot), held


class StreamTests(TestCase):
    """The update streams each agent holds open now, against its cap."""

    def _streams(self, client_id: str = AGENT):
        (standing,) = [found for found in agent_activity.standings() if found.identity.client_id == client_id]
        return standing.streams

    def test_an_agent_holding_none_has_none_and_is_not_full(self):
        _seen(AGENT)

        streams = self._streams()

        self.assertEqual(streams.open, 0)
        self.assertFalse(streams.full)

    def test_an_agent_holding_one_has_it_counted_against_the_cap(self):
        _seen(AGENT)
        _seen(OTHER)
        _listening(AGENT, 1)

        streams = self._streams()

        self.assertEqual((streams.open, streams.cap), (1, settings.SEVERINO_AGENT_STREAMS_AT_ONCE))
        self.assertFalse(streams.full)
        self.assertEqual(self._streams(OTHER).open, 0, "a stream is its own agent's")

    def test_an_agent_at_the_cap_is_full_as_its_next_stream_is_refused(self):
        _seen(AGENT)
        _listening(AGENT, settings.SEVERINO_AGENT_STREAMS_AT_ONCE)

        streams = self._streams()

        self.assertEqual(streams.open, streams.cap)
        self.assertTrue(streams.full)
        refused = agent_limits.LIMITS.listen(AGENT)
        self.assertIsInstance(refused, agent_limits.Limited)
        self.assertEqual(refused.code, agent_limits.TOO_MANY_CALLS)


class QueryBudgetTests(TestCase):
    def _queries(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            agent_activity.standings()
        return len(captured.captured_queries)

    def test_reading_does_not_grow_with_agents_persons_or_history(self):
        _seen(AGENT)
        _did(AGENT)
        few = self._queries()
        for index in range(6):
            client = f"example-agent-{index}"
            # Each its own person, with a role and without.
            _seen(client, person=_person(f"person-{index}", roles.Role.VIEWER if index % 2 else ""))
            for _ in range(8):
                _did(client)
                denials.record_denial(interface="api", reason="forbidden", actor=client)

        self.assertEqual(self._queries(), few)


class PageTests(TestCase):
    def test_each_agent_row_names_its_person_and_their_role(self):
        _seen(AGENT, person=_person("kim", roles.Role.VIEWER))
        _seen(OTHER, person=_person("nobody"))
        self.client.force_login(get_user_model().objects.create_user("operator"))

        page = self.client.get(reverse("agent_policy"))

        self.assertContains(page, 'data-role="viewer"')
        self.assertContains(page, "kim")
        self.assertContains(page, 'data-role="none"')

    def test_a_row_says_its_open_streams_and_warns_only_at_the_cap(self):
        _seen(AGENT)
        _seen(OTHER)
        _seen("example-quiet")
        _listening(AGENT, 1)
        _listening(OTHER, settings.SEVERINO_AGENT_STREAMS_AT_ONCE)
        self.client.force_login(get_user_model().objects.create_user("operator"))

        page = self.client.get(reverse("agent_policy")).content.decode()

        rows = {client: _row(page, client) for client in (AGENT, OTHER, "example-quiet")}
        self.assertIn('data-streams="open"', rows[AGENT])
        self.assertNotIn('data-streams="full"', rows[AGENT])
        self.assertIn('data-streams="full"', rows[OTHER])
        self.assertNotIn("data-streams", rows["example-quiet"], "an agent holding none shows nothing more")

    def test_the_refused_headline_counts_what_the_rows_count(self):
        _seen(AGENT)
        denials.record_denial(interface="mcp", reason=RATE_LIMITED, actor=AGENT)
        refused = AuditLog.objects.get(metadata__reason=RATE_LIMITED)
        refused.metadata = {**refused.metadata, denials.COUNT: 14}
        refused.save(update_fields=["metadata"])
        denials.record_denial(interface="mcp", reason="forbidden", actor=AGENT)
        self.client.force_login(get_user_model().objects.create_user("operator"))

        page = self.client.get(reverse("agent_policy"))

        rows = sum(standing.activity.refused for standing in page.context["standings"])
        self.assertEqual(page.context["refused_count"], rows)
        self.assertEqual(rows, 15)


def _row(page: str, client_id: str) -> str:
    """The markup of ``client_id``'s row on the Agents page."""

    start = page.index(f'data-agent="{client_id}"')
    return page[start : page.index("</tr>", start)]
