"""Real-browser layout checks, run by the browser:* tasks (`mise run browser`).

No module here matches Django's default test*.py discovery, so the plain suite
needs neither Playwright nor a browser, and this package imports nothing. `mise
run browser` runs it, and `mise run ci` always does. Imports point down:
widths, selectors and the probes, then survey, reading and serving, then the
gate, then the stress estate and the layout rules, then the tests that run
them.

Pages render through their real views over three synthetic example.* estates,
a sparse one (core/tests/test_browser_fixtures.py), a dense, production-shaped
one (core/tests/test_browser_dense_fixtures.py), and every list filled with the
longest values its model holds (core/tests/test_browser_derived_fixtures.py),
then load in a browser whose every request is
answered locally: nothing here can reach a real endpoint. Assertions are
invariants rather than pixels, so a page can change and still pass as long as
it stays readable.

Every CSS selector the checks use is in SELECTORS (selectors.py), and
core/tests/test_browser_selectors.py fails when one names nothing the templates
render. JavaScript in the probes queries by tag only.
"""
