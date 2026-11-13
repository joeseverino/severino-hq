"""`lint:css` refuses each break of the stylesheet's contract.

Each sheet is handed to Stylelint under the name of a real one, so the
overrides for the tokens layer and the API reference apply as they do to the
file itself.
"""

import json
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]
CONFIG = "tools/lint/stylelint.config.mjs"
SUPPRESSIONS = "tools/lint/stylelint-suppressions.json"

APP = "static/css/app/fixture.css"
TOKENS = "static/css/app/tokens.css"
CUTS = "static/css/app/cuts.css"
CUT = "declaration-property-value-disallowed-list"
WHY = "/* stylelint-disable-next-line declaration-property-value-disallowed-list"
REFERENCE = "static/css/api-reference.css"
TABLES = "static/css/app/tables.css"
SIZED = "rule-selector-property-disallowed-list"
# A text's case, which only a kicker over a group sets.
CASE = SIZED
ROLE = "selector-disallowed-list"
RESTYLED = "hq/restyled-primitive"

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
    (APP, ".facts dt { text-transform: uppercase; }", "rule-selector-property-disallowed-list"),
    (APP, ".band-label, .x { text-transform: uppercase; }", "rule-selector-property-disallowed-list"),
    (APP, ".kpi .label { text-transform: uppercase; }", "rule-selector-property-disallowed-list"),
    (APP, ".job-state { text-transform: uppercase; }", CASE),
    (APP, ".a { text-transform: none; }", CASE),
    (APP, ".a { font-variant-caps: all-small-caps; }", CASE),
    (APP, ".eyebrow .a { text-transform: uppercase; }", CASE),
    (APP, ".eyebrow-a { text-transform: uppercase; }", CASE),
    (APP, ".eyebrow, .a { text-transform: uppercase; }", CASE),
    (APP, ".month-grid thead { text-transform: uppercase; }", CASE),
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
    (APP, ".row-columns dt { text-transform: uppercase; }", SIZED),
    (REFERENCE, ".a { color: #fff; }", "color-no-hex"),
    (REFERENCE, ".a { background: color-mix(in srgb, var(--bg), var(--ink)); }", "function-disallowed-list"),
    (REFERENCE, ".a { font-family: serif; }", "property-disallowed-list"),
    (REFERENCE, ".a { font-size: 1.1em; }", "declaration-property-value-disallowed-list"),
    (REFERENCE, ".a { color: var(--no-such-token); }", "no-unknown-custom-properties"),
    (APP, '.a::before { content: "\\203A"; }', "declaration-property-value-disallowed-list"),
    (APP, ".a::after { content: ' / '; }", "declaration-property-value-disallowed-list"),
    (TOKENS, '.a::after { content: "Dismissed: "; }', "declaration-property-value-disallowed-list"),
    (REFERENCE, '.a::after { content: "\\2197"; }', "declaration-property-value-disallowed-list"),
    (APP, ".a { opacity: .62; }", "hq/faded-text"),
    (APP, ".a { opacity: 50%; }", "hq/faded-text"),
    (APP, ".a { opacity: var(--fade); } :root { --fade: .5; }", "hq/faded-text"),
    (APP, ".btn:disabled, .btn-note { opacity: .5; }", "hq/faded-text"),
    (APP, ".a:not(:disabled) { opacity: .5; }", "hq/faded-text"),
    (APP, ".day :is(.label, .month-mark) { opacity: .6; }", "hq/faded-text"),
    (APP, ".month-mark span { opacity: .6; }", "hq/faded-text"),
    (APP, "@keyframes example-fade { to { opacity: .4; } }", "hq/faded-text"),
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
    (APP, ".kv.a-variant { gap: 0; }", RESTYLED),
    (APP, ".a-page > .section-body { margin: 0; }", RESTYLED),
    (APP, ".a-page .msg-error { color: var(--ink); }", RESTYLED),
    (APP, ".a-table th { color: var(--ink); }", RESTYLED),
    (APP, ".a-page { .card { margin: 0; } }", RESTYLED),
    (APP, ".a-page { & > .notice { margin: 0; } }", RESTYLED),
    (TABLES, ".a-page .btn { color: var(--ink); }", RESTYLED),
]

PASSED = [
    (APP, ".a { color: var(--ink); background: color-mix(in srgb, var(--bg) 50%, transparent); }"),
    (APP, ".a { position: sticky; background: color-mix(in srgb, var(--ground) 90%, transparent); }"),
    (APP, ".a { position: sticky; background: var(--surface-2); }"),
    (APP, ".a::before { background: linear-gradient(transparent, var(--ground) 35%); }"),
    (APP, ".a { border-color: currentColor; outline-color: inherit; }"),
    (APP, ".a { font-size: .9em; padding: 7px 1px; gap: calc(var(--space-2) + 8px); }"),
    (APP, ".pill-green { color: var(--ok); }"),
    (APP, ".eyebrow { text-transform: uppercase; }"),
    (TABLES, ".month-grid th,\n.calendar-weekday { text-transform: uppercase; letter-spacing: .08em; }"),
    (TABLES, ".eyebrow { text-transform: uppercase; }"),
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
    (APP, ".a { opacity: 0; } .b { opacity: 1; } .c { opacity: 100%; }"),
    (APP, '.btn:disabled, span.btn[aria-disabled="true"] { opacity: .52; }'),
    (APP, 'main[aria-busy="true"] :is(.a, .b) { opacity: .55; }'),
    (APP, ".chart:hover rect:not(:hover) { opacity: .78; } .route-map:hover .route-line:not(:hover) { opacity: .55; }"),
    (APP, ".day.is-past .month-mark, .day.is-past .month-item::before { opacity: .62; }"),
    (APP, "@keyframes conn-pulse { 50% { opacity: .35; } }"),
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
    (REFERENCE, ".scalar-app .section-header { color: var(--ink); }"),
    (APP, ".a { overflow: auto; } .b { overflow-x: visible; overflow-y: scroll; }"),
    (APP, f"{WHY} -- a frame's corners clip its fill */\n.a {{ overflow: hidden; }}"),
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

    def test_each_sheets_restyled_count_is_what_the_rule_finds(self):
        """A sheet's count of restyled primitives only falls: a fix lowers it in
        tools/lint/stylelint-suppressions.json, which Stylelint reads as a ceiling alone."""

        with tempfile.TemporaryDirectory() as scratch:
            unsuppressed = Path(scratch, "none.json")
            unsuppressed.write_text("{}", encoding="utf-8")
            found = Path(scratch, "found.json")
            subprocess.run(
                [
                    "stylelint",
                    "--config",
                    CONFIG,
                    "--formatter",
                    "json",
                    f"--suppress-location={unsuppressed}",
                    f"--output-file={found}",
                    "static/css/**/*.css",
                ],
                capture_output=True,
                cwd=ROOT,
                check=False,
            )
            report = json.loads(found.read_text(encoding="utf-8"))
        counted = Counter(
            Path(file["source"]).relative_to(ROOT).as_posix()
            for file in report
            for warning in file["warnings"]
            if warning["rule"] == RESTYLED
        )
        recorded = json.loads((ROOT / SUPPRESSIONS).read_text(encoding="utf-8"))
        self.assertEqual(
            {sheet: rules[RESTYLED]["count"] for sheet, rules in recorded.items() if RESTYLED in rules},
            dict(counted),
            f"record each sheet's count: stylelint --config {CONFIG} --suppress={RESTYLED} --suppress-location={SUPPRESSIONS} 'static/css/**/*.css'",
        )
