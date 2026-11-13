import re

from django import forms
from django.test import SimpleTestCase
from django.utils.safestring import mark_safe

from hq.platform.application.entity_links import EntityLink
from hq.platform.application.printable import Unprintable
from hq.platform.application.tests.markup import all_elements, links, text, texts, with_class
from hq.platform.core.templatetags.value_tags import breakable, cell_line, code_spans, entity, or_empty, readout


def pieces(html):
    """What a token may wrap between: its text split at each break opportunity."""

    return [re.sub(r"</?span[^>]*>", "", piece) for piece in str(html).split('<span class="seam"></span>')]


def wholes(html):
    """The parts a value keeps on one line, as they read."""

    return [whole.words for whole in with_class(str(html), "whole")]


def boxed(html):
    """The parts of a phrase in a box of their own, which a cell cuts."""

    return [part.words for part in with_class(str(html), "part")]


def held(html):
    """The runs of a token held on one line together."""

    return re.findall(r'<span class="joined">([^<]*)</span>', str(html))


class BreakableTests(SimpleTestCase):
    def test_a_dotted_line_draws_its_dot_for_the_eye_alone(self):
        # As labels.dotted joins it.
        html = f'<a href="/x/">{breakable("Example Host\xa0· Hosting")}</a>'

        self.assertEqual(links(html), [("Example Host Hosting", "/x/")])
        self.assertEqual(text(html), "Example Host · Hosting")

    def test_a_dot_stays_on_the_line_of_the_name_before_it(self):
        """A name as wide as its line leaves a dot after its box no room
        there, so the dot is inside the box and the line breaks after it."""

        html = str(breakable("wildcard-example-com\xa0· Dec 4"))
        (name,) = with_class(html, "token")
        self.assertTrue(name.text.rstrip().endswith("·"), html)
        self.assertEqual(text(html), "wildcard-example-com · Dec 4")
        self.assertIn('aria-hidden="true">·</span>', html)

    def test_a_rate_is_one_figure(self):
        html = str(breakable("faster than 10:36/mi"))
        self.assertNotIn('class="token"', html)
        self.assertEqual(text(html), "faster than 10:36/mi")

    def test_a_quantity_standing_alone_is_one_figure(self):
        """A quantity is the whole value of a figure cell: drawn as a name, a
        cell would cut it or break it, and its table holds no line for it."""

        for value in ("$43,708.78", "−\u2060$11,676.82", "5/5", "9:59/mi", "10:10/mi", "−0.73%", "+12.5%", "1,204"):
            with self.subTest(value=value):
                html = str(breakable(value))
                self.assertNotIn('class="token"', html)
                self.assertEqual(text(html), value)

    def test_an_address_standing_alone_is_still_a_name(self):
        for value in ("172.18.0.2", "10.0.0.10:8000", "2.39.8", "10.0.0.0/24"):
            with self.subTest(value=value):
                self.assertIn("data-sole", breakable(value))

    def test_a_word_a_value_ends_on_stays_on_the_last_line_of_the_name_before_it(self):
        """Held in the name's own box, after the parts a cell cuts."""

        html = str(breakable("_example-verification.example.com TXT"))
        (name,) = with_class(html, "token")
        self.assertTrue(name.text.endswith("com\xa0TXT"), html)
        self.assertEqual(boxed(html), ["_example-verification."])
        self.assertEqual(text(html), "_example-verification.example.com TXT")

    def test_a_word_a_phrase_ends_on_stays_in_the_box_of_the_part_before_it(self):
        """A line breaks at a box's edge whatever space follows it, so after
        a hyphenated part the word is inside the part's box, after the text
        a cell cuts."""

        html = str(breakable("pulled 2024.12.1-ubu2404-ls48 available"))
        (part,) = with_class(html, "whole")
        self.assertTrue(part.text.endswith("ls48\xa0available"), html)
        self.assertEqual(boxed(html), ["2024.12.1-ubu2404-ls48"])
        self.assertEqual(text(html), "pulled 2024.12.1-ubu2404-ls48 available")

    def test_an_address_is_whole_and_wraps_after_its_colon(self):
        self.assertEqual(pieces(breakable("10.0.0.10:8000")), ["10.0.0.10:", "8000"])

    def test_an_ipv6_address_wraps_after_a_colon_run_never_inside_it(self):
        self.assertEqual(pieces(breakable("2001:db8::100")), ["2001:", "db8::", "100"])

    def test_an_image_reference_wraps_at_its_seams_never_inside_its_version(self):
        self.assertEqual(pieces(breakable("portainer/agent:2.39.8")), ["portainer/", "agent:", "2.39.8"])

    def test_a_hostname_and_a_path_wrap_after_their_dots_and_slashes(self):
        self.assertEqual(pieces(breakable("app.dev.example.com/api")), ["app.dev.", "example.com/", "api"])

    def test_a_scrap_of_a_name_stays_with_its_neighbor(self):
        """A piece under three characters is never a line of its own."""

        self.assertEqual(pieces(breakable("2001:db8::1")), ["2001:", "db8::1"])
        self.assertEqual(pieces(breakable("a.example.io")), ["a.example.io"])
        self.assertEqual(held(breakable("portainer-agent-portainer-agent-1")), ["-1"])
        self.assertEqual(held(breakable("a-example-host")), ["a-example"])

    def test_a_hold_never_starts_or_ends_after_a_hyphen(self):
        """A break after a hyphen is a break opportunity, so an element's edge
        there lets a cut name break inside a part."""

        for name in ("portainer-agent-portainer-agent-1", "a-rewrite-a-sweep-skipped-01.example.com", "ab-c-d"):
            html = str(breakable(name))
            self.assertNotIn('-<span class="joined">', html)
            self.assertFalse([run for run in held(html) if run.endswith("-")], name)

    def test_a_name_never_wraps_to_leave_a_label_alone_at_either_end(self):
        """A hostname's last label stays with the one before it, and its first
        with the next while another seam is left to break at."""

        self.assertEqual(pieces(breakable("dev-hq.example.com")), ["dev-hq.", "example.com"])
        self.assertEqual(pieces(breakable("www.app.dev.example.com")), ["www.app.", "dev.", "example.com"])
        self.assertEqual(pieces(breakable("example.com")), ["example.com"])

    def test_a_hyphenated_first_label_keeps_its_seam(self):
        """Held to the next label, a hyphenated one could break only at a hyphen."""

        self.assertEqual(
            pieces(breakable("example-docker-host-01.tailc0ffee.ts.net")),
            ["example-docker-host-01.", "tailc0ffee.", "ts.net"],
        )
        self.assertEqual(
            pieces(breakable("example-primary-container-host-with-a-long-name.example.ts.net.")),
            ["example-primary-container-host-with-a-long-name.", "example.", "ts.net."],
        )
        self.assertEqual(pieces(breakable("hq.tailc0ffee.ts.net")), ["hq.tailc0ffee.", "ts.net"])

    def test_a_name_among_other_words_keeps_every_seam(self):
        """A value of several words shares its line, so holding a label there
        only widens the column it sits in."""

        self.assertEqual(pieces(breakable("on dev-hq.example.com"))[-1], "com")
        self.assertEqual(pieces(breakable("['dev-hq.example.com']"))[-1], "com&#x27;]")

    def test_a_url_wraps_after_its_scheme_never_inside_it(self):
        self.assertEqual(pieces(breakable("https://example.com/api")), ["https://", "example.com/", "api"])

    def test_a_url_query_wraps_after_its_question_mark_and_each_ampersand(self):
        self.assertEqual(
            pieces(breakable("example.com/items/?q=not+read&status=&source=")),
            ["example.com/", "items/?", "q=not+read&amp;", "status=&amp;", "source="],
        )

    def test_markup_in_the_value_stays_text(self):
        self.assertNotIn("<b>", str(breakable("<b>x</b>.y")))
        self.assertEqual("".join(pieces(breakable("<b>x</b>.y"))), "&lt;b&gt;x&lt;/b&gt;.y")

    def test_a_form_field_passes_as_the_markup_it_renders(self):
        class Form(forms.Form):
            key = forms.CharField(widget=forms.HiddenInput, initial="web:example-key")

        field = Form()["key"]
        self.assertEqual(str(breakable(field)), str(field))

    def test_nothing_is_added_at_either_end(self):
        self.assertEqual(pieces(breakable("dev/app.hidden:")), ["dev/", "app.hidden:"])

    def test_a_hyphenated_part_is_kept_whole(self):
        self.assertEqual(wholes(breakable("sudo /usr/severino-hq/bin")), ["severino-hq/"])

    def test_a_part_of_a_phrase_is_its_own_box_and_a_name_alone_is_not(self):
        self.assertEqual(boxed(breakable("restart example-web now")), ["example-web"])
        self.assertEqual(boxed(breakable("example-web")), [])

    def test_a_range_of_figures_in_a_phrase_is_held_on_one_line(self):
        """A range reads as one figure: no seam inside it, and a part of its
        own, which moves whole and breaks at its dash only when longer than
        its line."""

        printed = str(breakable("10.5–12.5 mi"))
        self.assertNotIn('class="token"', printed)
        # Its unit inside its box, since the line breaks at a box's edge.
        self.assertEqual(wholes(printed), ["10.5–12.5 mi"])

    def test_a_value_ended_by_a_dot_holds_it_inside_its_last_box(self):
        """A box is a place a line may break even before a no-break space,
        so the dot that follows a value is drawn inside the value's last
        box, hidden from a reader, and the line breaks only after it."""

        printed = str(breakable("Relayed via example-region", "·"))
        (dot,) = [element for element in all_elements(printed) if element.attrs.get("aria-hidden") == "true"]
        self.assertEqual(dot.text, "·")
        self.assertIn("token", {name for box in dot.ancestors for name in box.classes})
        # Held to the word by a no-break space, and the line breaks after it.
        self.assertIn("region&nbsp;<span", printed)
        self.assertTrue(printed.endswith(" ") and not printed.endswith("&nbsp; "))
        self.assertEqual(text(printed), "Relayed via example-region ·")

    def test_a_figure_and_the_word_it_counts_never_part_at_a_line(self):
        """A figure and its unit or counted word are one run, held by a
        no-break space, so the word never opens a line alone below its
        number; the line still breaks between the other words."""

        for phrase, run in (
            ("Due in 80 days", "80 days"),
            ("+0.01 m / 90 days", "90 days"),
            ("+0.01 m / 90 days", "+0.01 m /"),
            ("Commit › Revision", "Commit ›"),
            ("−2.5 km since", "−2.5 km"),
            ("used 3 GB of", "3 GB"),
            ("over 1 month", "1 month"),
            ("Moved 1 account.", "1 account."),
            ("up 12 % today", "12 %"),
            ("$12.00 each", "$12.00 each"),
            ("10.5–12.5 mi", "12.5 mi"),
            ("at 4.2 m/s", "4.2 m/s"),
        ):
            with self.subTest(phrase=phrase):
                drawn = re.sub(r"<[^>]*>", "", str(breakable(phrase)))
                self.assertIn(run.replace(" ", "\xa0"), drawn)
                self.assertEqual(text(breakable(phrase)), phrase)
        drawn = str(breakable("Due in 80 days"))
        self.assertIn("Due in ", drawn)
        self.assertEqual(str(breakable("12 of example.com")).count("\xa0"), 1)
        self.assertNotIn("\xa0", str(breakable("80 example.com")))

    def test_it_is_marked_a_token_and_nothing_is_one(self):
        self.assertIn('class="token"', breakable("example.com"))
        self.assertEqual(breakable(""), "")

    def test_each_word_is_a_token_of_its_own(self):
        """A name with spaces wraps at them; each word moves to the next line whole."""

        html = str(breakable("example-iphone-16-pro and example.com"))
        self.assertEqual(html.count('class="token"'), 2)
        self.assertEqual(re.sub(r"<[^>]+>", "", html), "example-iphone-16-pro and example.com")

    def test_only_a_token_that_is_the_whole_value_is_marked_sole(self):
        self.assertIn("data-sole", breakable("example-docker-host-01"))
        self.assertNotIn("data-sole", breakable("Receipt for example-docker-host-01"))

    def test_a_name_has_its_box_and_its_strut_side_by_side_in_its_token(self):
        """A name is drawn in a box of its own beside its strut, both in its
        token, so whatever lays the name out holds the strut that sizes it
        beside the box that draws it (cuts.css); a word of a sentence has
        neither, and a line of words is cut by the line's own box."""

        for name in ("example.com/example-path", "hq.example.com"):
            with self.subTest(name=name):
                html = str(breakable(name))
                (token,) = with_class(html, "token")
                (strut,) = with_class(html, "strut")
                (box,) = with_class(html, "cut")
                self.assertEqual(strut.ancestors[-1:], (token,))
                self.assertEqual(box.ancestors[-1:], (token,))
                self.assertEqual(strut.attrs.get("aria-hidden"), "true")
                self.assertEqual(strut.words, "")
                pieces = strut.attrs["data-strut"].split("\u200b")
                self.assertEqual("".join(pieces), name)
                self.assertGreater(len(pieces), 1)
        self.assertEqual(with_class(str(breakable("Receipt for example-docker-host-01")), "strut"), [])
        self.assertEqual(with_class(str(breakable("Receipt for example-docker-host-01")), "cut"), [])
        (line,) = with_class(str(cell_line("on example-web")), "cut")
        self.assertIn("cell-line", line.ancestors[-1].classes)
        (named,) = with_class(str(cell_line("example-web")), "cut")
        self.assertIn("token", named.ancestors[-1].classes)

    def test_a_run_too_long_to_be_a_word_is_held_as_a_name(self):
        """A digest has no seam to wrap at, so a cell cuts it as it cuts a
        hyphenated name: in a phrase a box of its own, alone a name with a
        box to cut it, never plain text that widens its column."""

        digest = "697a431011ba77cec8b221767743484b20217b9786894fea67f222af655f40a7"
        self.assertEqual(boxed(breakable(f"{digest} on example-oidc")), [digest, "example-oidc"])
        self.assertIn('<span class="cut"><span class="whole">', breakable(digest))
        self.assertEqual(boxed(breakable(f"sha256:{digest} pulled")), [digest])
        self.assertEqual(breakable("Example Finance"), "Example Finance")

    def test_a_word_that_breaks_nowhere_stays_text(self):
        self.assertEqual(breakable("Example Finance"), "Example Finance")
        self.assertEqual(breakable("Import a Health export (.zip)"), "Import a Health export (.zip)")

    def test_an_entity_name_is_a_token(self):
        link = EntityLink(label="example-docker-host", url="/machines/example-docker-host/", kind_label="Machine")
        self.assertIn('<span class="whole">example-docker-host</span>', entity({}, link))

    def test_a_value_or_its_mark_holds_the_names_in_its_text(self):
        self.assertEqual(wholes(or_empty("on example-web")), ["example-web"])
        self.assertIn("empty-value", or_empty(None))
        self.assertEqual(or_empty(0), 0)
        named = type("Named", (), {"__str__": lambda self: "example-operator"})()
        with self.assertRaises(Unprintable):
            or_empty(named)
        self.assertEqual(or_empty(mark_safe("<b>x-y</b>")), "<b>x-y</b>")

    def test_a_cell_line_holds_each_name_in_it(self):
        """Each hyphenated name in the line is a part kept whole; the words
        between them stay text."""

        line = "example-docker-host · example-web-frontend"
        html = str(cell_line(line, "/containers/x/"))
        self.assertEqual(wholes(html), line.split(" · "))
        self.assertIn('href="/containers/x/"', html)
        self.assertNotIn("<a", str(cell_line(line)))

    def test_a_readout_identifier_is_a_breakable_token(self):
        self.assertEqual(pieces(readout("app.example.com:443")), ["<code>app.", "example.com:", "443</code>"])
        self.assertEqual(readout("11"), "11")
        self.assertEqual(boxed(readout("restart example-web now")), ["example-web"])


class FigureWordTests(SimpleTestCase):
    """A value tag that writes a figure with its words joins them as
    ``breakable`` does, with no markup, so it also serves in an attribute."""

    def render(self, source: str, **context) -> str:
        from django.template import Context, Template

        return Template("{% load value_tags %}" + source).render(Context(context))

    def test_each_value_tag_keeps_its_figure_and_word_on_one_line(self):
        for source, context, run in (
            ('{{ n|counted:"sign-in" }}', {"n": 3}, "3\xa0sign-ins"),
            ('{{ n|counted:"check passes,checks pass" }}', {"n": 2}, "2\xa0checks pass"),
            ('{{ x|floatformat:2|measured:"mi" }}', {"x": 3.5712}, "3.57\xa0mi"),
            ('{{ n|measured:"more" }}', {"n": 4}, "4\xa0more"),
            ("{{ n|bytes }}", {"n": 3 * 1024**3}, "3.0\xa0GB"),
            ("{{ n|seconds }}", {"n": 273}, "4\xa0min 33\xa0s"),
        ):
            with self.subTest(source=source):
                drawn = self.render(source, **context)
                self.assertEqual(drawn, run)

    def test_a_missing_measure_is_the_missing_mark_without_its_unit(self):
        drawn = self.render('{{ x|measured:"mi" }}', x=None)
        self.assertIn('class="empty-value"', drawn)
        self.assertNotIn("mi", drawn)

    def test_the_join_is_breakables_own(self):
        from hq.platform.core.templatetags.line_breaks import held

        for phrase in ("Due in 80 days", "+0.01 m / 90 days", "3 sign-ins", "12 of example.com", "80 example.com"):
            with self.subTest(phrase=phrase):
                self.assertEqual(held(phrase), re.sub(r"<[^>]*>", "", str(breakable(phrase))))


class CodeSpanTests(SimpleTestCase):
    """A description's Markdown code spans read as code; nothing else in it is markup."""

    def test_each_span_is_code_and_the_prose_around_it_reads_as_written(self):
        html = code_spans("Read `hq://hq` first, then `get_resource` for one record.")

        self.assertEqual(texts(html, "code"), ["hq://hq", "get_resource"])
        self.assertEqual(text(html), "Read hq://hq first, then get_resource for one record.")

    def test_markup_inside_and_outside_a_span_is_text(self):
        html = code_spans('<b>bold</b> and `<script>alert("x")</script>` <img src=x onerror=y>')

        self.assertEqual({element.tag for element in all_elements(html)} - {"span"}, {"code"})
        self.assertEqual(texts(html, "code"), ['<script>alert("x")</script>'])
        self.assertEqual(text(html), '<b>bold</b> and <script>alert("x")</script> <img src=x onerror=y>')

    def test_a_backtick_without_its_pair_stays_a_backtick(self):
        for written in ("An unclosed `name", "`a` and one more `", "``"):
            with self.subTest(written):
                html = code_spans(written)
                self.assertEqual(text(html).count("`"), written.count("`") - 2 * len(texts(html, "code")))

        self.assertEqual(texts(code_spans("An unclosed `name"), "code"), [])

    def test_the_punctuation_after_a_span_is_held_inside_its_last_box(self):
        # Outside the span's box, a line may break between the box and the
        # mark, which then starts the next line alone.
        for name in ("get_resource", "hq://hq"):
            for mark in (";", ":", ",", ".", ")", ")."):
                with self.subTest(name=name, mark=mark):
                    html = str(code_spans(f"Call `{name}`{mark} then stop"))

                    (held,) = with_class(html, "mark")
                    self.assertEqual(held.text, mark)
                    self.assertIn("code", {ancestor.tag for ancestor in held.ancestors})
                    # No box closes between the span's last letter and its mark.
                    self.assertTrue(html[: html.index('<span class="mark"')].endswith(name[-1]))
                    self.assertEqual(texts(html, "code"), [name])
                    self.assertEqual(text(html), f"Call {name}{mark} then stop")

    def test_prose_without_a_span_is_drawn_as_breakable_draws_it(self):
        self.assertEqual(text(code_spans("Lists every host.")), text(breakable("Lists every host.")))


class LeadActionTests(SimpleTestCase):
    """A narrow head keeps the control a page is for, not the one that comes first."""

    def lead(self, *actions):
        from hq.platform.core.templatetags.action_tags import head_actions

        beside, _menu = head_actions(actions)
        return beside[0].label

    def test_a_destructive_control_never_leads_while_another_is_offered(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(
            self.lead(PageAction("Stop", "/stop/", danger=True), PageAction("Restart", "/restart/", method="post")),
            "Restart",
        )

    def test_one_that_can_be_pressed_leads_over_one_that_cannot(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(
            self.lead(PageAction("Start", "/start/", disabled=True), PageAction("Restart", "/restart/")), "Restart"
        )

    def test_the_primary_leads_among_the_rest(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(self.lead(PageAction("Domain", "/d/"), PageAction("Add", "/a/", primary=True)), "Add")

    def test_with_only_a_destructive_control_it_leads(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(self.lead(PageAction("Remove", "/r/", danger=True)), "Remove")


class HeadActionsTests(SimpleTestCase):
    """A head draws two controls beside its title and offers the rest in its menu."""

    def split(self, *actions):
        from hq.platform.core.templatetags.action_tags import head_actions

        beside, menu = head_actions(actions)
        return [action.label for action in beside], [action.label for action in menu]

    def test_the_lead_and_the_next_stand_beside_the_title_and_a_destructive_one_waits(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(
            self.split(
                PageAction("Restart", "/restart/", method="post"),
                PageAction("Stop", "/stop/", danger=True),
                PageAction("Edit", "/edit/"),
                PageAction("Remove", "/remove/", danger=True),
            ),
            (["Restart", "Edit"], ["Edit", "Stop", "Remove"]),
        )

    def test_the_menu_holds_every_control_but_the_lead_the_second_first(self):
        from hq.platform.application.pages import PageAction

        beside, menu = self.split(*(PageAction(label, f"/{label}/") for label in "abcd"))
        self.assertEqual(beside, ["a", "b"])
        self.assertEqual(menu, ["b", "c", "d"])

    def test_one_control_has_no_menu_and_none_draws_nothing(self):
        from hq.platform.application.pages import PageAction

        self.assertEqual(self.split(PageAction("Edit", "/edit/")), (["Edit"], []))
        self.assertEqual(self.split(), ([], []))
