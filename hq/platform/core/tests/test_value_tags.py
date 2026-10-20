import re

from django.test import SimpleTestCase

from hq.platform.application.entity_links import EntityLink
from hq.platform.core.templatetags.value_tags import breakable, entity, readout


def pieces(html):
    """What a token may wrap between: its text split at each break opportunity."""

    return re.sub(r"</?span[^>]*>", "", str(html)).split("<wbr>")


class BreakableTests(SimpleTestCase):
    def test_an_address_is_whole_and_wraps_after_its_colon(self):
        self.assertEqual(pieces(breakable("10.0.0.10:8000")), ["10.0.0.10:", "8000"])

    def test_an_ipv6_address_wraps_after_a_colon_run_never_inside_it(self):
        self.assertEqual(pieces(breakable("2001:db8::1")), ["2001:", "db8::", "1"])

    def test_an_image_reference_wraps_at_its_seams_never_inside_its_version(self):
        self.assertEqual(pieces(breakable("portainer/agent:2.39.8")), ["portainer/", "agent:", "2.39.8"])

    def test_a_hostname_and_a_path_wrap_after_their_dots_and_slashes(self):
        self.assertEqual(pieces(breakable("app.example.com/a")), ["app.", "example.", "com/", "a"])

    def test_a_url_wraps_after_its_scheme_never_inside_it(self):
        self.assertEqual(pieces(breakable("https://example.com/a")), ["https://", "example.", "com/", "a"])

    def test_markup_in_the_value_stays_text(self):
        self.assertNotIn("<b>", str(breakable("<b>x</b>.y")))
        self.assertEqual("".join(pieces(breakable("<b>x</b>.y"))), "&lt;b&gt;x&lt;/b&gt;.y")

    def test_nothing_is_added_at_either_end(self):
        self.assertEqual(pieces(breakable("a.hidden:")), ["a.", "hidden:"])

    def test_a_hyphenated_part_is_kept_whole(self):
        self.assertIn('<span class="whole">severino-hq/</span>', breakable("sudo /usr/severino-hq/bin"))

    def test_it_is_marked_a_token_and_nothing_is_one(self):
        self.assertIn('class="token"', breakable("example.com"))
        self.assertEqual(breakable(""), "")

    def test_each_word_is_a_token_of_its_own(self):
        """A name with spaces wraps at them; each word moves to the next line whole."""

        html = str(breakable("example-iphone-16-pro and example.com"))
        self.assertEqual(html.count('class="token"'), 2)
        self.assertEqual(re.sub(r"<[^>]+>", "", html), "example-iphone-16-pro and example.com")

    def test_a_word_that_breaks_nowhere_stays_text(self):
        self.assertEqual(breakable("Example Finance"), "Example Finance")
        self.assertEqual(breakable("Import a Health export (.zip)"), "Import a Health export (.zip)")

    def test_an_entity_name_is_a_token(self):
        link = EntityLink(label="example-docker-host-01", url="/machines/example-docker-host-01/", kind_label="Machine")
        self.assertIn('<span class="whole">example-docker-host-01</span>', entity({}, link))

    def test_a_readout_identifier_is_a_breakable_token(self):
        self.assertEqual(pieces(readout("app.example.com:443")), ["<code>app.", "example.", "com:", "443</code>"])
        self.assertEqual(readout("11"), "11")


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
