"""One face for a queue card, without changing what its owner emitted."""

from copy import deepcopy
from dataclasses import asdict

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.decisions import decision, urgency, workflow_parts
from hq.platform.application.item_help import cannot_step, commands, do_step, run_step
from hq.platform.application.workflow_contracts import ActionLink, WorkflowOutcome, WorkflowPlan, WorkflowStep

from .markup import elements, hrefs, select, text, texts, with_class, within


def card(item) -> str:
    return render_to_string("partials/_work_queue.html", {"items": [item]})


class DecisionTests(SimpleTestCase):
    def item(self, *, actions=(), steps=(), **fields):
        return {
            "label": "Example needs a decision",
            "status": "attention",
            "source": "Example",
            "detail": "What it means for the reader.",
            "count": 1,
            "actions": [asdict(action) for action in actions],
            "workflow": asdict(
                WorkflowPlan(
                    "example",
                    "What to do",
                    steps,
                    WorkflowOutcome("claim_absent", "example", ""),
                )
            )
            if steps
            else None,
            **fields,
        }

    def test_the_first_available_step_leads_without_mutating_or_repeating_it(self):
        action = ActionLink("fix", "Review change", "read", "/example/review/", recommended=True)
        verify = ActionLink("verify", "Recheck", "write", "/example/check/", method="POST")
        item = self.item(
            steps=(
                WorkflowStep("act", "Fix it", "", "recommended", (action,)),
                WorkflowStep("verify", "Confirm", "Check the outcome", "after_action", (verify,)),
            )
        )
        before = deepcopy(item)
        shown = decision(item)
        self.assertEqual(item, before)
        self.assertEqual(shown["actions"], [asdict(action)])
        self.assertEqual([step["phase"] for step in shown["workflow"]["steps"]], ["verify"])
        html = card(item)
        self.assertEqual(hrefs(html).count("/example/review/"), 1)
        (recheck,) = select(html, "formaction", "/example/check/")
        self.assertEqual(recheck.attrs["form"], "hq-post")
        self.assertEqual(elements(html, "form"), [])

    def test_what_is_wrong_and_its_buttons_are_on_the_face_of_the_card(self):
        fix = ActionLink("fix", "Restore the example", "remote_write", "/example/fix/", recommended=True)
        check = ActionLink("verify", "Check again", "read", "/example/check/", method="POST")
        html = card(
            self.item(
                steps=(
                    WorkflowStep("act", "Fix it", "", "recommended", (fix,)),
                    WorkflowStep("verify", "Check that it worked", "", "after_action", (check,)),
                )
            )
        )

        self.assertEqual(elements(html, "details") + elements(html, "ol"), [])
        for words in ("What it means for the reader.", "Restore the example", "Check again"):
            self.assertIn(words, text(html))

    def test_a_lone_check_is_a_button_and_never_a_step(self):
        check = ActionLink("verify", "Check again", "read", "/example/check/", method="POST")
        shown = decision(self.item(steps=(WorkflowStep("verify", "Check again", "", "available", (check,)),)))

        self.assertEqual(shown["actions"], [asdict(check)])
        self.assertEqual(shown["steps"], [])
        self.assertIsNone(shown["workflow"])

    def test_a_check_stands_beside_the_owners_own_action(self):
        own = ActionLink("open", "Open", "read", "/example/")
        check = ActionLink("verify", "Check again", "read", "/example/check/", method="POST")
        shown = decision(
            self.item(actions=(own,), steps=(WorkflowStep("verify", "Check again", "", "available", (check,)),))
        )

        self.assertEqual(shown["actions"], [asdict(own), asdict(check)])

    def test_a_reason_is_a_sentence_and_never_a_numbered_step(self):
        item = self.item(steps=(cannot_step("Only the registrar can renew it."),))
        shown = decision(item)
        html = card(item)

        self.assertEqual(shown["reason"], "Only the registrar can renew it.")
        self.assertEqual(shown["steps"], [])
        self.assertIn("Only the registrar can renew it.", texts(html, "p"))
        self.assertEqual([step for step in within(html, "data-attention-item") if step.tag in {"ol", "li"}], [])
        self.assertNotIn("Why HQ cannot", text(html))

    def test_one_command_stands_alone_and_several_are_numbered(self):
        one = card(self.item(steps=(run_step("On the machine", "example --fix"),)))
        two = card(
            self.item(
                steps=(
                    run_step("On the machine", "example --fix"),
                    do_step("Then sign in again."),
                )
            )
        )

        self.assertEqual([steps.tag for steps in with_class(one, "resolution-workflow")], ["ul"])
        self.assertIn("example --fix", texts(one, "code"))
        self.assertEqual([steps.tag for steps in with_class(two, "resolution-workflow")], ["ol"])
        self.assertIn("Then sign in again.", text(two))
        self.assertEqual(elements(one, "details") + elements(two, "details"), [])

    def test_long_steps_fold_and_short_ones_do_not(self):
        long = commands("k", (("In the repository", "example " + "--flag " * 40),))
        many = commands("k", tuple((f"Step {n}", "example") for n in range(3)))

        self.assertFalse(workflow_parts(commands("k", (("Here", "example"),)))["fold"])
        for plan in (long, many):
            self.assertTrue(workflow_parts(asdict(plan))["fold"])
            # The queue holds every step in the row's opened line, long or short.
            html = card({**self.item(), "workflow": asdict(plan)})
            (below,) = with_class(html, "row-below")
            self.assertIn("example", below.words)

    def test_an_opened_row_reads_its_meaning_in_full_and_keeps_its_buttons_together(self):
        links = tuple(ActionLink(f"open-{n}", f"Open page {n}", "read", f"/example/{n}/") for n in range(3))
        html = card(self.item(actions=links))

        # The meaning in full opens beneath the title, across the line.
        (below,) = with_class(html, "row-below")
        self.assertIn("What it means for the reader.", below.words)
        # Every button, the first two and those that wait, is in one cell.
        cells = {
            id(next(ancestor for ancestor in reversed(link.ancestors) if ancestor.tag == "td"))
            for link in elements(html, "a")
            if link.attrs.get("href", "").startswith("/example/")
        }
        self.assertEqual(len(cells), 1)
        self.assertEqual(sorted(hrefs(html)), [f"/example/{n}/" for n in range(3)])

    def test_the_title_goes_to_the_thing_and_the_named_next_step_is_a_link(self):
        subject = asdict(ActionLink("subject", "example-host", "read", "/machines/example-host/"))
        about = decision(self.item(url="/findings/?rule=example", subject=subject))
        backlog = decision(self.item(url="/assets/?missing=1", action="Fill them in"))
        advice = decision(self.item(url="/runs/", action="Count this run as a hard session."))

        self.assertEqual(about["href"], "/machines/example-host/")
        self.assertEqual(backlog["href"], "/assets/?missing=1")
        self.assertEqual([(a["label"], a["url"]) for a in backlog["actions"]], [("Fill them in", "/assets/?missing=1")])
        self.assertEqual((advice["actions"], advice["advice"]), ([], "Count this run as a hard session."))

    def test_a_card_says_since_when_only_when_its_owner_knows(self):
        self.assertNotIn("Since", text(card(self.item())))
        known = card(self.item(since="2026-10-03T09:29:15+00:00"))
        self.assertIn("Since", text(known))
        self.assertEqual(len(elements(known, "time")), 1)

    def test_a_dismissed_card_is_its_title(self):
        fix = ActionLink("fix", "Restore the example", "remote_write", "/example/fix/", recommended=True)
        html = card(self.item(actions=(fix,), aside=True))

        self.assertIn("Example needs a decision", text(html))
        self.assertNotIn("What it means for the reader.", text(html))
        self.assertNotIn("Restore the example", text(html))

    def test_urgency_is_said_in_one_set_of_words(self):
        self.assertEqual((urgency("serious"), urgency("attention")), ("Urgent", "Needs attention"))
        for item, said in ((self.item(status="serious"), "Urgent"), (self.item(notice=True), "Notice")):
            (pill,) = select(card(item), "data-origin")
            self.assertEqual(pill.words, said)

    def test_owner_actions_win_and_only_identical_method_and_url_are_removed(self):
        get = ActionLink("open", "Open", "read", "/example/")
        post = ActionLink("run", "Run", "write", "/example/", method="POST")
        shown = decision(
            self.item(
                actions=(get,), steps=(WorkflowStep("act", "Act", "Keep this explanation", "recommended", (get, post)),)
            )
        )
        self.assertEqual(shown["actions"], [asdict(get)])
        self.assertEqual(shown["workflow"]["steps"][0]["actions"], [asdict(post)])
        self.assertEqual(shown["workflow"]["steps"][0]["summary"], "Keep this explanation")

    def test_a_blocked_or_later_step_is_never_promoted(self):
        action = ActionLink("run", "Run", "write", "/example/", method="POST")
        for state in ("blocked", "after_action"):
            with self.subTest(state=state):
                item = self.item(steps=(WorkflowStep("act", "Act", "", state, (action,)),))
                self.assertEqual(decision(item)["actions"], [])
                self.assertEqual([step["label"] for step in decision(item)["steps"]], ["Act"])
                self.assertEqual(len(decision(item)["steps"][0]["actions"]), 1)

    def test_a_fully_promoted_plan_does_not_leave_an_empty_disclosure(self):
        action = ActionLink("open", "Review", "read", "/example/")
        shown = decision(self.item(steps=(WorkflowStep("act", "Act", "", "available", (action,)),)))
        self.assertIsNone(shown["workflow"])

    def test_destructive_post_actions_keep_the_shared_safety_and_escaping(self):
        action = ActionLink("remove", "Remove <example>", "destructive", "/example/remove/", method="POST")
        html = render_to_string("partials/_action_links.html", {"actions": (action,)})
        (remove,) = select(html, "formaction", "/example/remove/")
        self.assertLessEqual({"ghost", "danger"}, remove.classes)
        self.assertEqual(remove.words, "Remove <example>")
        self.assertEqual(elements(html, "example"), [])
        self.assertNotIn("/example/remove/", hrefs(html))


class OriginTests(SimpleTestCase):
    def test_a_finding_says_its_rule_and_the_line_it_is_declared_at(self):
        from hq.platform.application.findings import RULES

        rule = RULES[0]
        found = decision({"key": f"hq.example:finding:{rule.name}:node:1", "source": "Example", "status": "serious"})

        self.assertEqual((found["origin"]["rule"], found["origin"]["title"]), (rule.name, rule.title))
        path, _, line = found["origin"]["where"].rpartition(":")
        self.assertTrue(path.startswith("hq/") and path.endswith(".py"), path)
        self.assertTrue(line.isdigit())

    def test_anything_else_says_where_it_came_from(self):
        found = decision({"key": "example.domain:Renewals:Passport", "source": "Example", "status": "attention"})

        self.assertEqual(found["origin"], {"source": "Example", "key": "example.domain:Renewals:Passport"})

    def test_the_pill_opens_its_origin_and_nothing_else_does(self):
        item = decision({"key": "example.domain:x", "source": "Example", "status": "attention", "label": "X"})

        html = render_to_string("partials/_urgency_pill.html", {"item": item})

        (pill,) = select(html, "data-origin")
        (panel,) = select(html, "id", pill.attrs["data-origin"])
        self.assertIn("popover", panel.attrs)
