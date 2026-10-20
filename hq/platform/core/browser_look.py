"""A page as the browser gates draw it, for a person or an agent to look at.

`mise run dev:look -- <pages>` opens pages at a few widths, saves a screenshot
of each under var/look/, and, given `--rule`, runs that rule of
`LayoutBrowserTests` there and prints what it finds. `--css` adds a stylesheet
first: how a new rule is shown to fail on the defect it was written for before
it lands. Not a gate: it fails only when the rule it was asked to run fails.

A page is one of the stress estate's (sparse and dense), the API reference
(`api/docs`, `api/docs/dark`), or a page the example estate's navigation
reaches, named for its address under `estate` (`estate`, `estate/finance`).
Only the sources the pages asked for are rendered.
"""

import os
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings

from hq.platform.core.browser_tests import (
    ALL_PAGES,
    PHONE_SCREEN,
    REFERENCE,
    REFERENCE_DARK,
    LayoutBrowserTests,
    render_reference,
    wait_for_reference,
)
from hq.platform.core.estate_browser_tests import ExampleEstateBrowserTests, navigation, render_estate

OUT = Path(settings.BASE_DIR) / "var" / "look"
REFERENCE_PAGES = (REFERENCE, REFERENCE_DARK)
ESTATE = "estate"


def _usage(variable: str) -> str:
    """What the task was given: mise exports each argument and flag of a
    task's usage as ``usage_<name>``, lower case, and an unset flag as nothing
    at all."""

    return os.environ.get(variable, "")


def _listed(name: str) -> list[str]:
    return [part.strip() for part in _usage(name).split(",") if part.strip()]


def _estate_name(path: str) -> str:
    return "/".join(filter(None, (ESTATE, path.strip("/"))))


def _is_estate(name: str) -> bool:
    return name == ESTATE or name.startswith(f"{ESTATE}/")


def _estate_path(name: str) -> str:
    rest = name.removeprefix(ESTATE).strip("/")
    return f"/{rest}/" if rest else "/"


class Look(LayoutBrowserTests, ExampleEstateBrowserTests):
    """The layout gate's rules and pages, with the example estate's way of
    loading a page whose parts arrive after it."""

    @classmethod
    def render(cls):
        estate = [_estate_name(path) for path in navigation()]
        known = [*ALL_PAGES, *REFERENCE_PAGES, *estate]
        pages = _listed("usage_pages")
        if pages == ["all"]:
            pages = known
        unknown = sorted(set(pages) - set(known))
        if unknown:
            raise AssertionError(f"No page {unknown}. Pages: {', '.join(known)}")
        cls.asked = pages
        rendered: dict = {}
        if set(pages) & set(ALL_PAGES):
            rendered |= LayoutBrowserTests.render()
        if set(pages) & set(REFERENCE_PAGES):
            rendered |= render_reference()
        # The example estate answers by address and part, as its parts are fetched.
        # A rule reads a page's markup by its name.
        cls.answers = render_estate()[1] if set(pages) & set(estate) else {}
        return rendered | {_estate_name(address): body for (address, part), body in cls.answers.items() if not part}

    def respond(self, route):
        parts = urlsplit(route.request.url)
        address = parts.path + (f"?{parts.query}" if parts.query else "")
        answer = self.answers.get((address, route.request.headers.get("x-fragment", "")))
        if answer is not None:
            route.fulfill(content_type="text/html", body=answer)
        else:
            LayoutBrowserTests.respond(self, route)

    def open(self, name, width):
        if _is_estate(name):
            self.visit(_estate_path(name), width)
            return
        super().open(name, width)
        if name in REFERENCE_PAGES and not _usage("usage_no_script"):
            wait_for_reference(self.page)

    def test_look(self):
        widths, rule = self.asked_for()
        self.start(java_script_enabled=not _usage("usage_no_script"))
        if _usage("usage_dark"):
            self.page.emulate_media(color_scheme="dark")
        OUT.mkdir(parents=True, exist_ok=True)
        failed = [found for name in self.asked for width in widths if (found := self.look(name, width, rule))]
        for failure in failed:
            print(failure)
        if rule:
            self.assertEqual(failed, [], f"{rule} does not hold")
            print(f"{rule} holds on every page at {', '.join(map(str, widths))}")

    def asked_for(self):
        """The widths and rule the task was given, each checked."""

        widths = [int(width) for width in _listed("usage_widths")] or [PHONE_SCREEN[0], 820, 1360]
        rule = _usage("usage_rule")
        self.assertTrue(not rule or callable(getattr(self, rule, None)), f"No rule named {rule!r}")
        return widths, rule

    def look(self, name, width, rule):
        """Screenshot ``name`` at ``width``, and what ``rule`` finds there."""

        self.open(name, width)
        if css := _usage("usage_css"):
            self.page.add_style_tag(content=css)
        if _usage("usage_open"):
            self.open_every_disclosure(name)
        if _usage("usage_expand"):
            self.page.evaluate(
                "() => document.querySelectorAll('main button[data-row-toggle]').forEach((toggle) => toggle.click())"
            )
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
