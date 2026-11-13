"""`lint:css` refuses each break of the stylesheet's contract.

Each sheet is handed to Stylelint under the name of a real one, so the
overrides for the tokens layer and the API reference apply as they do to the
file itself.
"""

import json
import shutil
import subprocess
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]
CONFIG = "tools/lint/stylelint.config.mjs"

APP = "static/css/app/fixture.css"
TOKENS = "static/css/app/tokens.css"
CUTS = "static/css/app/cuts.css"
CUT = "declaration-property-value-disallowed-list"
WHY = "/* stylelint-disable-next-line declaration-property-value-disallowed-list"
REFERENCE = "static/css/api-reference.css"
TABLES = "static/css/app/tables.css"
SIZED = "rule-selector-property-disallowed-list"
# A text's case, which only a kicker over a group sets, saying so in a disable comment.
CASE = "property-disallowed-list"
ROLE = "selector-disallowed-list"
RESTYLED = "hq/restyled-primitive"
# A cap that stops text at a reading measure.
MEASURE = CUT
# A run of sections laid out by an auto-fit grid of its own, not the flow.
FLOW = "hq/flow-grid"
# Each exception is written on the line before the one it exempts.
KICKER = f"/* stylelint-disable-next-line {CASE}"
LAYS_OUT = f"/* stylelint-disable-next-line {FLOW}"

REFUSED = [
    (APP, ".a { color: #0a0a0a; }", "color-no-hex"),
    (APP, ".a { --tone: #fff; }", "color-no-hex"),
    (APP, ".a { fill: tomato; }", "color-named"),
    (APP, ".a { border-color: canvas; }", "declaration-property-value-disallowed-list"),
    (APP, ".a { background: oklch(0.5 0.1 200); }", "function-disallowed-list"),
    (APP, ".a { box-shadow: 0 1px RGBA(0, 0, 0, .2); }", "function-disallowed-list"),
    (APP, ".a { width: var(--no-such-token); }", "no-unknown-custom-properties"),
    (APP, ".a { font-size: 13px; }", "declaration-property-value-disallowed-list"),
    (APP, ".a { font-size: .8rem; }", "declaration-property-value-disallowed-list"),
    (APP, ".a { padding: 0 8px; }", "declaration-property-value-disallowed-list"),
    (APP, ".a { column-gap: 12px; }", "declaration-property-value-disallowed-list"),
    (APP, ".a { margin-inline-start: 24px; }", "declaration-property-value-disallowed-list"),
    (APP, ".facts dt { text-transform: uppercase; }", CASE),
    (APP, ".job-state { text-transform: uppercase; }", CASE),
    (APP, ".a { text-transform: none; }", CASE),
    (APP, ".a { font-variant-caps: all-small-caps; }", CASE),
    (APP, ".eyebrow { text-transform: uppercase; }", CASE),
    (TABLES, ".a { text-transform: uppercase; }", CASE),
    (TOKENS, ".a { text-transform: uppercase; }", CASE),
    (REFERENCE, ".a { text-transform: uppercase; }", CASE),
    (APP, ".a { color: var(--ink); } .a { color: var(--muted); }", "no-duplicate-selectors"),
    (APP, ".data-table td > .a { min-width: 20ch; }", SIZED),
    (APP, ".a-table .row-detail .a { max-width: 72ch; }", SIZED),
    (APP, ".data-table .a > li { flex: none; }", SIZED),
    (APP, ".a,\n.data-table > tbody .b { inline-size: 40ch; }", SIZED),
    (APP, ".a th .b { flex-basis: 12rem; }", SIZED),
    (APP, ".row-columns { grid-template-columns: 1fr 1fr; }", SIZED),
    (APP, ".row-columns dt { text-transform: uppercase; }", CASE),
    (REFERENCE, ".a { color: #fff; }", "color-no-hex"),
    (REFERENCE, ".a { background: color-mix(in srgb, var(--bg), var(--ink)); }", "function-disallowed-list"),
    (REFERENCE, ".a { font-family: serif; }", "property-disallowed-list"),
    (REFERENCE, ".a { font-size: 1.1em; }", "declaration-property-value-disallowed-list"),
    (REFERENCE, ".a { color: var(--no-such-token); }", "no-unknown-custom-properties"),
    (APP, '.a::before { content: "\\203A"; }', "declaration-property-value-disallowed-list"),
    (APP, ".a::after { content: ' / '; }", "declaration-property-value-disallowed-list"),
    (TOKENS, '.a::after { content: "Dismissed: "; }', "declaration-property-value-disallowed-list"),
    (REFERENCE, '.a::after { content: "\\2197"; }', "declaration-property-value-disallowed-list"),
    (APP, ".a { position: sticky; background: var(--bg); }", "hq/ground-cover"),
    (
        APP,
        ".a { position: sticky; background: color-mix(in srgb, var(--surface) 90%, transparent); }",
        "hq/ground-cover",
    ),
    (APP, ".a::before { background: linear-gradient(transparent, var(--bg) 35%); }", "hq/ground-cover"),
    (APP, ".a { overflow: hidden; }", CUT),
    (APP, ".a { overflow-x: clip; }", CUT),
    (APP, ".a { overflow: auto hidden; }", CUT),
    (APP, ".a { text-overflow: ellipsis; }", CUT),
    (APP, ".a { -webkit-line-clamp: 2; }", CUT),
    (APP, ".a { line-clamp: 2; }", CUT),
    (TOKENS, ".a { overflow: hidden; }", CUT),
    (TOKENS, ":root { --measure: 80ch; }", MEASURE),
    (TOKENS, ":root { --prose-measure: 40rem; }", MEASURE),
    (APP, ".a p { max-width: 72ch; }", MEASURE),
    (APP, ".a { max-inline-size: min(100%, 65ch); }", MEASURE),
    (CUTS, ".a { max-width: 30ch; }", MEASURE),
    (REFERENCE, ".a { max-inline-size: 60ch; }", MEASURE),
    (APP, ".a { grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr)); }", FLOW),
    (APP, ".a { grid-template-columns: repeat( AUTO-FILL , 12rem); }", FLOW),
    (APP, ".a { grid: auto-flow / repeat(auto-fit, minmax(10rem, 1fr)); }", FLOW),
    (APP, ".two-col .a { grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); }", FLOW),
    (APP, ".a { &:has(> *) { grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); } }", FLOW),
    (APP, ".form { grid-template-columns: repeat(auto-fill, minmax(min(100%, 18rem), 1fr)); }", FLOW),
    (
        APP,
        f"{LAYS_OUT} -- a form's fields */\n.a {{ grid-template-columns: repeat(3, 1fr); }}",
        "--report-needless-disables",
    ),
    ("static/css/app/layout.css", ".a { grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr)); }", FLOW),
    (APP, f"{WHY} */\n.a {{ overflow: hidden; }}", "--report-descriptionless-disables"),
    (APP, f"{WHY} -- a frame's corners */\n.a {{ overflow: auto; }}", "--report-needless-disables"),
    (TABLES, ".data-table td .whole:not(td > strong:first-child *) { white-space: nowrap; }", ROLE),
    (TABLES, ".data-table td .whole:not(td > a:first-child > strong *) { white-space: nowrap; }", ROLE),
    (TABLES, ".data-table td:has(> strong:first-child) { color: var(--ink); }", ROLE),
    (TABLES, ".data-table td .whole:not(.cell-data *) { white-space: nowrap; }", ROLE),
    (TABLES, ".data-table td .a, .b { white-space: nowrap; }", ROLE),
    (TABLES, ".row-detail .attention-title .token { white-space: nowrap; }", ROLE),
    (CUTS, ".data-table td .service-name strong > [data-entity] { overflow: hidden; }", ROLE),
    (APP, ".a-page .btn { color: var(--ink); }", RESTYLED),
    (APP, ".facts-list.a-variant { gap: 0; }", RESTYLED),
    (APP, ".a-page > .section-body { margin: 0; }", RESTYLED),
    (APP, ".a-page .notice-error { color: var(--ink); }", RESTYLED),
    (APP, ".a-table th { color: var(--ink); }", RESTYLED),
    (APP, ".a-page { .card { margin: 0; } }", RESTYLED),
    (APP, ".a-page { & > .notice { margin: 0; } }", RESTYLED),
    (TABLES, ".a-page .btn { color: var(--ink); }", RESTYLED),
    (CUTS, ".data-table td .btn > .token > .a-page { max-width: 100%; }", RESTYLED),
]

PASSED = [
    (APP, ".a { color: var(--ink); background: color-mix(in srgb, var(--bg) 50%, transparent); }"),
    (APP, ".a { position: sticky; background: color-mix(in srgb, var(--ground) 90%, transparent); }"),
    (APP, ".a { position: sticky; background: var(--surface-2); }"),
    (APP, ".a::before { background: linear-gradient(transparent, var(--ground) 35%); }"),
    (APP, ".a { border-color: currentColor; outline-color: inherit; }"),
    (APP, ".a { font-size: .9em; padding: 7px 1px; gap: calc(var(--space-2) + 8px); }"),
    (APP, ".pill-green { color: var(--ok); }"),
    (APP, f"{KICKER} -- the kicker primitive, over a group */\n.eyebrow {{ text-transform: uppercase; }}"),
    (
        TABLES,
        f"{KICKER} -- a kicker: the weekday row */\n.month-grid th, .calendar-weekday {{ text-transform: uppercase; }}",
    ),
    (
        APP,
        ".data-table { width: 100%; } .a-list > li { flex: none; } .data-table td code { padding: 0; } .width-td { width: 1rem; }",
    ),
    (
        TABLES,
        ".data-table td code { min-width: 0; max-width: 100%; } .row-columns { grid-template-columns: max-content minmax(0, 1fr); }",
    ),
    (TOKENS, ":root { --x: #fff; --y: rgb(0 0 0); --z: canvas; --w: tomato; }"),
    (APP, '.a::before { content: "\\203A" / ""; } .b::after { content: "/" / ""; }'),
    (APP, '.a::before { content: "Dismissed: " / "Dismissed: "; } .b::before { content: ""; }'),
    (
        APP,
        '.a::before { content: none; } .b::before { content: counter(step) ". "; } .c::before { content: attr(data-x); }',
    ),
    (CUTS, ".a { overflow: hidden; text-overflow: ellipsis; } .b { -webkit-line-clamp: 2; line-clamp: 2; }"),
    (
        TABLES,
        (
            ".data-table td .whole:not([data-cell~=name] *, code *) { white-space: nowrap; }"
            " .data-table.a-table td .token { overflow-wrap: anywhere; }"
            " .data-table td strong > [data-entity] { color: var(--ink); }"
        ),
    ),
    (APP, ".data-table td strong { font-weight: 600; }"),
    (
        APP,
        (
            ".card > .kpi-grid { gap: 0; } .two-col > section:not(.section) { margin: 0; }"
            " .btn:not(.a-page *) { color: var(--ink); } .btn.is-busy { cursor: wait; }"
        ),
    ),
    ("static/css/app/page.css", ".btn.a-variant { color: var(--ink); }"),
    (CUTS, ".data-table td .btn > .token:not([data-sole]) > .whole { overflow: hidden; }"),
    (REFERENCE, ".scalar-app .section-header { color: var(--ink); }"),
    (APP, ".a { overflow: auto; } .b { overflow-x: visible; overflow-y: scroll; }"),
    (APP, ".a { max-width: 100%; max-inline-size: 24rem; inline-size: 16ch; } .b { max-width: max-content; }"),
    (APP, f"{WHY} -- a frame's corners clip its fill */\n.a {{ overflow: hidden; }}"),
    (APP, ".a { grid-template-columns: repeat(3, minmax(0, 1fr)); }"),
    (
        APP,
        f"{LAYS_OUT} -- a form's fields */\n.form {{ grid-template-columns: repeat(auto-fill, minmax(min(100%, 18rem), 1fr)); }}",
    ),
    (
        APP,
        f".a {{\n  {LAYS_OUT} -- a timeline's entries */\n  &:has(> :nth-child(2)) {{ grid-template-columns: repeat(auto-fit, 10rem); }}\n}}",
    ),
]


def lint(sheet: str, css: str) -> list[str]:
    """The rules Stylelint reports for `css`, read as one file named `sheet`.

    Each case is its own file, so no case shares a selector with another."""

    result = subprocess.run(
        ["stylelint", "--config", CONFIG, "--formatter", "json", "--stdin-filename", sheet],
        input=css,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    # Stylelint writes the report to stderr when it finds a problem.
    report = json.loads(result.stdout or result.stderr)
    return [warning["rule"] for file in report for warning in file["warnings"]]


@tag("source")
class StylesheetLintTests(SimpleTestCase):
    def setUp(self):
        self.assertTrue(shutil.which("stylelint"), "`mise run setup:lint-tools` installs Stylelint")

    def test_each_break_of_the_contract_is_refused(self):
        for sheet, css, rule in REFUSED:
            with self.subTest(sheet=sheet, css=css):
                self.assertIn(rule, lint(sheet, css))

    def test_what_the_contract_allows_passes(self):
        for sheet, css in PASSED:
            with self.subTest(sheet=sheet, css=css):
                self.assertEqual(lint(sheet, css), [])
