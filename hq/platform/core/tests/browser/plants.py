"""Every survey check held to its own examples: the plants it must find and the markup it must pass."""

import json
from typing import override

from hq.platform.core.tests.browser.gate import BrowserGate
from hq.platform.core.tests.browser.probes import Plant
from hq.platform.core.tests.browser.reading import probe, taken
from hq.platform.core.tests.browser.survey import CHECKS, examples, findings_of


def unmatched(finds, found):
    """What of ``finds`` no finding holds, and the findings left over, each
    finding standing for at most one of ``finds``: a matching found by
    augmenting paths, so a finding that holds two texts is never spent on
    the wrong one."""

    # The text each finding stands for, by the finding's place.
    taker: dict[int, int] = {}

    def take(place, seen):
        for at, line in enumerate(found):
            if at in seen or finds[place] not in str(line):
                continue
            seen.add(at)
            if at not in taker or take(taker[at], seen):
                taker[at] = place
                return True
        return False

    missing = [want for place, want in enumerate(finds) if not take(place, set())]
    return missing, [line for at, line in enumerate(found) if at not in taker]


class CheckExampleTests(BrowserGate):
    """Each check's examples, each drawn alone on a page of its own, since an
    example's markup reaches the whole page (a box placed in the window, a
    modal that makes the rest inert) and so would reach another's. The
    pages load one after another in one context with script and one
    without, every request answered from memory."""

    # A probe page is no page `dev:look` draws.
    looked_at = False

    @classmethod
    @override
    def render(cls):
        return {page: probe(example.markup, **example.values) for page, (_check, example, _probe) in examples().items()}

    def test_every_check_finds_its_plants_and_passes_its_clear(self):
        """A plant is found as it says, each finding once and nothing more;
        markup a check must pass draws no finding of it."""

        drawn = examples()
        self.assertTrue(drawn, "no check declares an example")
        for scripted in (False, True):
            self.start(java_script_enabled=scripted)
            for page, (check, example, read) in drawn.items():
                if example.scripted != scripted:
                    continue
                with self.subTest(rule=check, page=page, width=example.width):
                    self.open(page, example.width)
                    self.settle()
                    if example.setup:
                        self.page.evaluate(example.setup)
                        self.settle()
                    probes = [read, *([example.premise] if example.premise else [])]
                    outcome, *premise = self.timed_outcomes(probes)
                    if premise and (short := taken(premise[0])):
                        self.fail(f"{page} falls short of the case it is drawn for: {json.dumps(short)}")
                    returned, value = outcome[:2]
                    found = findings_of(check, returned, value, CHECKS[check].part, page)
                    finds = example.finds if isinstance(example, Plant) else ()
                    missing, more = unmatched(finds, found)
                    if missing or more:
                        self.fail(
                            f"{check} on {page}: {json.dumps({'not found': missing, 'found beside': more}, indent=1)}"
                        )
