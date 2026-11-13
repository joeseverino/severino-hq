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

    def test_a_word_a_value_ends_on_stays_on_the_last_line_of_the_name_before_it(self):
        """Held in the name's own box, after the parts a cell cuts."""

        html = str(breakable("_example-verification.example.com TXT"))
        (name,) = with_class(html, "token")
        self.assertTrue(name.text.endswith("com\xa0TXT"), html)
        self.assertEqual(boxed(html), ["_example-verification."])
        self.assertEqual(text(html), "_example-verification.example.com TXT")

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
        self.assertEqual(wholes(printed), ["10.5–12.5"])

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

    def test_a_name_a_cell_may_cut_inside_a_part_has_a_box_to_cut_it(self):
        """The box sits inside the token, so the token can ask for the name's
        whole length around it; a name with no hyphenated part, or a word of
        a sentence, has nothing to cut inside and no box."""

        cut = '<span class="token" data-sole><span class="cut"><span class="whole">'
        self.assertIn(cut, breakable("example-docker-host-01"))
        self.assertNotIn('class="cut"', breakable("hq.example.com"))
        self.assertNotIn('class="cut"', breakable("Receipt for example-docker-host-01"))
        self.assertIn('class="cut"', cell_line("on example-web"))

    def test_a_name_a_cell_may_cut_carries_an_unseen_strut_of_its_pieces(self):
        """A cell sizes its column by the strut, so it holds the longest piece
        an opened row cannot break; the strut says nothing a reader hears or
        copies, and a word of a sentence has none."""

        (strut,) = with_class(str(breakable("example.com/example-path")), "strut")
        self.assertEqual(strut.attrs.get("aria-hidden"), "true")
        self.assertEqual(strut.words, "")
        pieces = strut.attrs["data-strut"].split("\u200b")
        self.assertEqual("".join(pieces), "example.com/example-path")
        self.assertGreater(len(pieces), 1)
        self.assertEqual(with_class(str(breakable("Receipt for example-docker-host-01")), "strut"), [])

    def test_a_run_too_long_to_be_a_word_is_held_as_a_name(self):
        """A digest has no seam to wrap at, so a cell cuts it as it cuts a
        hyphenated name: in a phrase a box of its own, alone a name with a
        box to cut it, never plain text that widens its column."""

        digest = "697a431011ba77cec8b221767743484b20217b9786894fea67f222af655f40a7"
        self.assertEqual(boxed(breakable(f"{digest} on example-oidc")), [digest, "example-oidc"])
        self.assertIn('<span class="token" data-sole><span class="cut">', breakable(digest))
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
        self.assertEqual(wholes(readout("restart example-web now")), ["example-web"])


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
