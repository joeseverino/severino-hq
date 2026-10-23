import re

from django import forms
from django.test import SimpleTestCase
from django.utils.safestring import mark_safe

from hq.platform.application.entity_links import EntityLink
from hq.platform.application.printable import Unprintable
from hq.platform.application.tests.markup import with_class
from hq.platform.core.templatetags.value_tags import breakable, cell_line, entity, or_empty, readout


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
        pieces = strut.attrs["data-strut"].split("​")
        self.assertEqual("".join(pieces), "example.com/example-path")
        self.assertGreater(len(pieces), 1)
        self.assertEqual(with_class(str(breakable("Receipt for example-docker-host-01")), "strut"), [])

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
        between them stay text, and the title is the line unmarked."""

        line = "example-docker-host · example-web-frontend"
        html = str(cell_line(line, "/containers/x/"))
        self.assertEqual(wholes(html), line.split(" · "))
        self.assertIn(f'title="{line}"', html)
        self.assertIn('href="/containers/x/"', html)
        self.assertNotIn("<a", str(cell_line(line)))

    def test_a_readout_identifier_is_a_breakable_token(self):
        self.assertEqual(pieces(readout("app.example.com:443")), ["<code>app.", "example.com:", "443</code>"])
        self.assertEqual(readout("11"), "11")
        self.assertEqual(wholes(readout("restart example-web now")), ["example-web"])


class LeadActionTests(SimpleTestCase):
    """A narrow head keeps the control a page is for, not the one that comes first."""

    def lead(self, *actions):
        from hq.platform.core.templatetags.action_tags import lead_action

        return lead_action(actions).label

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
