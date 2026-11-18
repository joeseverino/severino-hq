"""One face for a queue card, without changing what its owner emitted."""

import re
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.utils import timezone

from hq.platform.application.action_links import read_link
from hq.platform.application.contracts import Effect
from hq.platform.application.decisions import decision
from hq.platform.application.finding_steps import Do, Press, Run
from hq.platform.application.markup_testing import all_elements, elements, hrefs, select, text, texts, with_class
from hq.platform.application.tests.links import example_command
from hq.platform.application.tone import Tone


def row_hrefs(html):
    """Every link's target."""

    return [link.attrs["href"] for link in elements(html, "a") if "href" in link.attrs]


def card(item) -> str:
    """A card's face: its markup less the urgency's panel, which opens apart
    from it (``OriginTests`` reads that)."""

    return re.sub(
        r"<dialog\b.*?</dialog>", "", render_to_string("partials/_work_queue.html", {"items": [item]}), flags=re.DOTALL
    )


class DecisionTests(SimpleTestCase):
    def item(self, *, actions=(), steps=(), check=None, **fields):
        return {
            "label": "Example needs a decision",
            "status": "attention",
            "source": "Example",
            "detail": "What it means for the reader.",
            "count": 1,
            "actions": [asdict(action) for action in actions],
            "steps": [asdict(step) for step in steps],
            "check": asdict(check) if check else None,
            **fields,
        }

    def test_the_press_leads_and_its_check_follows_without_mutating_or_repeating_either(self):
        action = read_link("fix", "Review change", "/example/review/", recommended=True)
        verify = example_command("verify", "Recheck", "write", "/example/check/", method="POST")
        item = self.item(steps=(Press(action),), check=verify)
        before = deepcopy(item)
        shown = decision(item)
        self.assertEqual(item, before)
        self.assertEqual(shown["actions"], [asdict(action), asdict(verify)])
        self.assertEqual(shown["steps"], [])
        html = card(item)
        self.assertEqual(row_hrefs(html).count("/example/review/"), 1)
        (recheck,) = select(html, "formaction", "/example/check/")
        self.assertEqual(recheck.attrs["form"], "hq-post")
        self.assertEqual(elements(html, "form"), [])

    def test_what_is_wrong_and_its_buttons_are_on_the_face_of_the_card(self):
        fix = example_command("fix", "Restore the example", "remote_write", "/example/fix/", recommended=True)
        check = example_command("verify", "Check again", Effect.READ, "/example/check/", method="POST")
        html = card(self.item(steps=(Press(fix),), check=check))

        self.assertEqual(elements(html, "details") + elements(html, "ol"), [])
        for words in ("What it means for the reader.", "Restore the example", "Check again"):
            self.assertIn(words, text(html))

    def test_a_lone_check_is_a_button_and_never_a_step(self):
        check = example_command("verify", "Check again", Effect.READ, "/example/check/", method="POST")
        shown = decision(self.item(check=check))

        self.assertEqual(shown["actions"], [asdict(check)])
        self.assertEqual(shown["steps"], [])

    def test_a_check_stands_beside_the_owners_own_action(self):
        own = read_link("open", "Open", "/example/")
        check = example_command("verify", "Check again", Effect.READ, "/example/check/", method="POST")
        shown = decision(self.item(actions=(own,), check=check))

        self.assertEqual(shown["actions"], [asdict(own), asdict(check)])

    def test_the_fix_and_the_named_page_stand_on_the_row_before_a_check(self):
        fix = example_command("remedy", "Renew certificate", "remote_write", "/commands/renew/", recommended=True)
        check = read_link("verify", "Check again", "/example/check/")
        shown = decision(
            self.item(url="/certificates/example/", action="Open certificate", steps=(Press(fix),), check=check)
        )

        self.assertEqual(
            [action["label"] for action in shown["actions"]], ["Renew certificate", "Open certificate", "Check again"]
        )

    def test_a_row_says_how_old_its_reading_is_only_once_it_is_out_of_date(self):
        from datetime import timedelta

        from django.utils import timezone

        from hq.platform.application.freshness import cadence

        kind = "github.repository"
        late = timezone.now() - cadence(kind).stale_after - timedelta(minutes=5)
        for at, shown in ((timezone.now(), False), (late, True)):
            with self.subTest(shown=shown):
                html = card(self.item(key="example:read", reads=kind, observed_at=at.isoformat()))

                self.assertEqual(bool(select(html, "data-reading-age")), shown)
                self.assertEqual("Read every" in text(html), shown)
        unknown = card(self.item(key="example:read", observed_at=late.isoformat()))
        self.assertFalse(select(unknown, "data-reading-age"))

    def test_the_named_page_leads_when_nothing_else_is_recommended(self):
        check = read_link("verify", "Check again", "/example/check/")
        shown = decision(self.item(url="/example/plan/", action="Upgrade plan", check=check))

        self.assertEqual(
            [action["label"] for action in shown["actions"] if action["recommended"]], ["Upgrade plan"]
        )

    def test_a_reason_is_a_sentence_and_never_a_numbered_step(self):
        item = self.item(no_help_reason="Only the registrar can renew it.")
        shown = decision(item)
        html = card(item)

        self.assertEqual(shown["reason"], "Only the registrar can renew it.")
        self.assertEqual(shown["steps"], [])
        self.assertIn("Only the registrar can renew it.", texts(html, "p"))
        self.assertEqual(with_class(html, "resolution-workflow"), [])
        self.assertNotIn("Why HQ cannot", text(html))

    def test_one_command_stands_alone_and_several_are_numbered(self):
        one = card(self.item(steps=(Run("On the machine", "example --fix"),)))
        two = card(self.item(steps=(Run("On the machine", "example --fix"), Do("Then sign in again."))))

        self.assertEqual([steps.tag for steps in with_class(one, "resolution-workflow")], ["ul"])
        self.assertIn("example --fix", texts(one, "code"))
        self.assertEqual([steps.tag for steps in with_class(two, "resolution-workflow")], ["ol"])
        self.assertIn("Then sign in again.", text(two))
        self.assertEqual(elements(one, "details") + elements(two, "details"), [])

    def test_a_steps_notes_are_said_under_it(self):
        html = card(self.item(steps=(Run("On the machine", "example --fix", ("Then press Refresh.",)),)))

        (steps,) = with_class(html, "resolution-workflow")
        self.assertIn("Then press Refresh.", steps.words)

    def test_the_queue_holds_every_step_in_the_rows_opened_line(self):
        many = tuple(Run(f"Step {n}", "example " + "--flag " * 40) for n in range(3))
        html = card(self.item(steps=many))

        (below,) = with_class(html, "row-below")
        self.assertIn("example", below.words)

    def test_a_row_reads_its_meaning_in_full_and_keeps_its_buttons_together(self):
        links = tuple(read_link(f"open-{n}", f"Open page {n}", f"/example/{n}/") for n in range(3))
        html = card(self.item(actions=links))

        # The meaning is the row's own, under its title; nothing beneath repeats it.
        (meaning,) = select(html, "data-attention-detail")
        self.assertIn("What it means for the reader.", meaning.words)
        self.assertEqual(with_class(html, "row-below"), [])
        # Every button stands in the row's one cell; none waits beneath it.
        buttons = [link for link in all_elements(html) if link.attrs.get("href", "").startswith("/example/")]
        cells = {
            id(next(ancestor for ancestor in reversed(link.ancestors) if ancestor.tag == "td")) for link in buttons
        }
        self.assertEqual(len(cells), 1)
        self.assertFalse([link for link in buttons if link.beneath])
        self.assertEqual(sorted(link.attrs["href"] for link in buttons), [f"/example/{n}/" for n in range(3)])

    def test_the_title_goes_to_the_thing_and_the_named_next_step_is_a_link(self):
        subject = asdict(read_link("subject", "example-host", "/machines/example-host/"))
        about = decision(self.item(url="/findings/?rule=example", subject=subject))
        backlog = decision(self.item(url="/assets/?missing=1", action="Fill in details"))
        advice = decision(self.item(url="/runs/", action="Count this run as a hard session."))

        self.assertEqual(about["href"], "/machines/example-host/")
        self.assertEqual(backlog["href"], "/assets/?missing=1")
        self.assertEqual(
            [(a["label"], a["url"]) for a in backlog["actions"]], [("Fill in details", "/assets/?missing=1")]
        )
        self.assertEqual((advice["actions"], advice["advice"]), ([], "Count this run as a hard session."))

    def test_a_card_says_since_when_only_when_its_owner_knows(self):
        self.assertNotIn("Since", text(card(self.item())))
        known = card(self.item(since="2026-10-03T09:29:15+00:00"))
        self.assertIn("Since", text(known))
        self.assertEqual(len(elements(known, "time")), 1)

    def test_a_dismissed_card_is_its_title(self):
        fix = example_command("fix", "Restore the example", "remote_write", "/example/fix/", recommended=True)
        html = card(self.item(actions=(fix,), aside=True))

        self.assertIn("Example needs a decision", text(html))
        self.assertNotIn("What it means for the reader.", text(html))
        self.assertNotIn("Restore the example", text(html))

    def test_urgency_is_said_in_one_set_of_words(self):
        self.assertEqual((Tone.SERIOUS.urgency, Tone.ATTENTION.urgency), ("Urgent", "Needs attention"))
        for item, said in ((self.item(status="serious"), "Urgent"), (self.item(notice=True), "Notice")):
            (pill,) = select(card(item), "data-origin")
            self.assertEqual(pill.words, said)

    def test_owner_actions_win_and_only_identical_method_and_url_are_removed(self):
        get = read_link("open", "Open", "/example/")
        post = example_command("run", "Run", "write", "/example/", method="POST")
        shown = decision(self.item(actions=(get,), steps=(Press(get), Press(post))))

        self.assertEqual(shown["actions"], [asdict(get), asdict(post)])

    def test_destructive_post_actions_keep_the_shared_safety_and_escaping(self):
        action = example_command("remove", "Remove <example>", "destructive", "/example/remove/", method="POST")
        html = render_to_string("partials/_action_links.html", {"actions": (action,)})
        (remove,) = select(html, "formaction", "/example/remove/")
        self.assertLessEqual({"ghost", "danger"}, remove.classes)
        self.assertEqual(remove.words, "Remove <example>")
        self.assertEqual(elements(html, "example"), [])
        self.assertNotIn("/example/remove/", hrefs(html))


class OriginTests(SimpleTestCase):
    def test_a_finding_says_its_rule_and_the_line_it_is_declared_at(self):
        from hq.platform.application.findings import RULES

        for rule in RULES:
            with self.subTest(rule=rule.name):
                key = f"hq.example:finding:{rule.name}:node:1"
                found = decision({"key": key, "source": "Example", "status": "serious"})

                self.assertEqual((found["origin"]["rule"], found["origin"]["title"]), (rule.name, rule.title))
                path, _, line = found["origin"]["where"].rpartition(":")
                self.assertTrue(path.startswith("hq/") and path.endswith(".py"), path)
                self.assertTrue(line.isdigit())

    def test_anything_else_says_why_and_where_it_came_from(self):
        why = "The passport expires within 90 days, and a renewal takes weeks."
        found = decision(
            {"key": "example.domain:Renewals:Passport", "source": "Example", "status": "attention", "why": why}
        )

        self.assertEqual(found["origin"]["why"], why)
        self.assertEqual(found["origin"]["source"], "Example")
        self.assertEqual(found["origin"]["key"], "example.domain:Renewals:Passport")

    def test_a_panel_is_named_apart_from_every_other_and_as_briefly_whatever_its_key(self):
        keys = ("example.domain:a", "example.domain:b", "hq.example:finding:example-rule:" + "machine:example-" * 12)
        panels = [decision({"key": key, "source": "Example", "status": "serious"})["origin"]["panel"] for key in keys]

        self.assertEqual(len(set(panels)), len(keys))
        self.assertEqual({len(panel) for panel in panels}, {len(panels[0])})

    def test_what_clears_an_item_follows_its_why(self):
        found = decision(
            {
                "key": "example.domain:space",
                "source": "Example",
                "status": "attention",
                "why": "Free on /: 1 GB of 30 GB.",
                "clears": "free space is back above 2 GB",
            }
        )

        why = found["origin"]["why"]
        self.assertTrue(why.startswith("Free on /: 1 GB of 30 GB."))
        self.assertIn("free space is back above 2 GB", why)
        self.assertTrue(why.endswith("."))

    def test_the_panel_lists_why_then_what_raised_it_and_dates_it_in_its_foot(self):
        why = "Example reads late, and a late reading is urgent."
        key = "example.domain:late"
        read = timezone.now() - timedelta(minutes=5)
        item = decision({"key": key, "source": "Example", "status": "serious", "label": "Late", "why": why})
        item["observed_at"] = read

        html = render_to_string("partials/_urgency_pill.html", {"item": item})
        html += render_to_string("partials/_urgency_pill.html#panel", {"item": item})

        (pill,) = select(html, "data-origin")
        self.assertTrue(pill.attrs["data-tip"].startswith(why))
        (panel,) = select(html, "id", pill.attrs["data-origin"])
        self.assertIn("popover", panel.attrs)
        panel_html = html[html.index("<dialog") :]
        # Nothing is folded away: every fact of what raised it is in view.
        self.assertEqual(elements(panel_html, "details"), [])
        labels = [found.words for found in with_class(panel_html, "fact-label")]
        values = [found.words for found in with_class(panel_html, "fact-value")]
        self.assertEqual(labels, ["Why", "Domain", "Key"])
        self.assertEqual(values, [why, "Example", key])
        # When it was read leaves the list for the panel's foot line.
        (foot,) = with_class(panel_html, "modal-foot")
        self.assertTrue(foot.words.startswith("Last read "))
        self.assertNotIn("Last read", " ".join(labels))

    def test_a_panel_with_nothing_to_date_draws_no_foot(self):
        item = decision({"key": "example.domain:x", "source": "Example", "status": "attention", "label": "X"})

        html = render_to_string("partials/_urgency_pill.html#panel", {"item": item})

        self.assertEqual(with_class(html, "modal-foot"), [])

    def test_the_pill_opens_its_origin_and_nothing_else_does(self):
        item = decision({"key": "example.domain:x", "source": "Example", "status": "attention", "label": "X"})

        html = render_to_string("partials/_urgency_pill.html", {"item": item})
        html += render_to_string("partials/_urgency_pill.html#panel", {"item": item})

        (pill,) = select(html, "data-origin")
        (panel,) = select(html, "id", pill.attrs["data-origin"])
        self.assertIn("popover", panel.attrs)
