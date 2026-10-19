"""A page as the browser gate draws it, for a person or an agent to look at.

`mise run dev:look -- <pages>` opens pages of the gate's own estate (sparse and
dense) at a few widths, saves a screenshot of each under var/look/, and, given
`--rule`, runs that rule of `LayoutBrowserTests` there and prints what it
finds. `--css` adds a stylesheet first: how a new rule is shown to fail on the
defect it was written for before it lands. Not a gate: it fails only when the
rule it was asked to run fails.
"""

import os
from pathlib import Path

from django.conf import settings

from hq.platform.core.browser_tests import ALL_PAGES, PHONE_SCREEN, LayoutBrowserTests

OUT = Path(settings.BASE_DIR) / "var" / "look"


def _listed(name: str) -> list[str]:
    return [part.strip() for part in os.environ.get(name, "").split(",") if part.strip()]


class Look(LayoutBrowserTests):
    def test_look(self):
        pages, widths, rule = self.asked()
        self.start(java_script_enabled=not os.environ.get("usage_no_script"))
        if os.environ.get("usage_dark"):
            self.page.emulate_media(color_scheme="dark")
        OUT.mkdir(parents=True, exist_ok=True)
        failed = [found for name in pages for width in widths if (found := self.look(name, width, rule))]
        for failure in failed:
            print(failure)
        if rule:
            self.assertEqual(failed, [], f"{rule} does not hold")
            print(f"{rule} holds on every page at {', '.join(map(str, widths))}")

    def asked(self):
        """The pages, widths and rule the task was given, each checked."""

        pages = _listed("usage_pages")
        if pages == ["all"]:
            pages = list(ALL_PAGES)
        unknown = sorted(set(pages) - set(ALL_PAGES))
        self.assertFalse(unknown, f"Not in the gate's estate: {unknown}. Pages: {', '.join(ALL_PAGES)}")
        widths = [int(width) for width in _listed("usage_widths")] or [PHONE_SCREEN[0], 820, 1360]
        rule = os.environ.get("usage_rule", "")
        self.assertTrue(not rule or callable(getattr(self, rule, None)), f"No rule named {rule!r}")
        return pages, widths, rule

    def look(self, name, width, rule):
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        self.open(name, width)
        if css := os.environ.get("usage_css"):
            self.page.add_style_tag(content=css)
        if os.environ.get("usage_open"):
            self.open_every_disclosure(name)
        if os.environ.get("usage_expand"):
            self.page.evaluate("() => document.querySelectorAll('main button[data-row-toggle]').forEach((toggle) => toggle.click())")
        shot = OUT / f"{name.replace('/', '-')}-{width}.png"
        self.page.screenshot(path=shot, full_page=True)
        print(shot)
        if not rule:
            return None
        try:
            getattr(self, rule)(name)
        except AssertionError as found:
            return f"{name} at {width}: {found}"
        return None
