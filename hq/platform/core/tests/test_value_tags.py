from django.test import SimpleTestCase

from hq.platform.core.templatetags.value_tags import breakable


class BreakableTests(SimpleTestCase):
    def test_an_address_wraps_between_its_parts_and_before_its_port(self):
        self.assertEqual(breakable("10.0.0.10:8000"), "10.<wbr>0.<wbr>0.<wbr>10<wbr>:8000")

    def test_a_hostname_and_a_path_wrap_after_their_dots_and_slashes(self):
        self.assertEqual(breakable("app.example.com/a"), "app.<wbr>example.<wbr>com/<wbr>a")

    def test_markup_in_the_value_stays_text(self):
        self.assertEqual(breakable("<b>x</b>.y"), "&lt;b&gt;x&lt;/<wbr>b&gt;.<wbr>y")

    def test_nothing_is_added_at_either_end(self):
        self.assertEqual(breakable(".hidden:"), ".<wbr>hidden:")

    def test_a_hyphenated_part_is_kept_whole(self):
        self.assertEqual(
            breakable("sudo /usr/severino-hq/bin"),
            'sudo /<wbr>usr/<wbr><span class="whole">severino-hq/</span><wbr>bin',
        )


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
