# Demo

HQ's own pages, filled with example data and saved as a static site, published
at the repository's GitHub Pages address. Same templates, CSS, scripts and font
as HQ; no page is made for the demo alone.

## Run it

| Command | What it does |
| --- | --- |
| `mise run demo:build [seed]` | Seeds a throwaway database, signs in, follows every link from the dashboard and saves each page to `var/demo` (another seed: `var/demo-<seed>`). |
| `mise run browser:demo` | Builds, then serves `var/demo` over plain HTTP and checks it in WebKit (below). Part of the `browser` gate, so every pull request runs it. |
| `mise run dev:demo [seed]` | Builds that seed and serves it on loopback, on a port the system picks; the log names the address. |

## Three seeds

| Seed | Built with | What it shows |
| --- | --- | --- |
| Showcase (default) | the example estate | A small homelab that has run for months: every reading present and current, every service set up, one expense still missing its receipt. Advanced HQ Posture is on. |
| Standard | `standard` | The same homelab with Advanced HQ Posture off. |
| Empty | `empty` | A fresh install: one signed-in user and nothing else. What someone sees the first time. |

## Two estates, two purposes

| Estate | Where | What it is for |
| --- | --- | --- |
| The example estate | `hq/platform/core/example_estate/` | The one homelab HQ is shown with: this demo, a dev database (`manage.py seed_demo`), the rendered-page and export tests, and the layout gate's realistic pass, which reads every page the navigation reaches. The checks and the demo read the same data. |
| The stress estate | `build_estate()` in `hq/platform/core/tests/test_browser_fixtures.py` | Long names, many rows, every state at once: it breaks things on purpose so the layout gate tests layouts at their worst. The demo never uses it. |

The example estate lives in `hq/`, not here, because `seed_demo` and the suite
run inside the image, which `demo/` is not copied into. `seed_demo` refuses a
database that already holds records rather than merging into it.

Everything in the seeds is `example.*` and private address ranges; tailnet
addresses are derived from the range HQ declares, never written out.

## How a page is built

Pages are asked for the way production answers them: over HTTPS, from a tailnet
laptop through the proxy, signed in with single sign-on only, with demo data
switched on (`as_production()` in the example estate), with static files
collected and named by content as the image build does, so a new build is never
read from a stale cache. A static host
cannot do four things HQ relies on, and each is handled here, never in HQ:

- **Partial page loads** send an `X-Fragment` header. The build saves each
  response under `__fragments__/` and `demo.js` serves it from there.
- **Addresses with a query** (a filter, a calendar day, Show all, a search,
  a Map node's details) are answered by HQ's server. The build saves each one
  a page links, a filter offers or a script reads under `__q/` and
  `__fragments__/`, and a search for each word of the pages' names;
  `demo-theme.js` opens the saved copy before the page paints, and `demo.js`
  says so on a page whose query has none. Each page carries the policy HQ
  sends for its own route, so the API reference loads.
- **Forms** have nowhere to post. Dismissals are kept in the browser's
  localStorage; any other form shows "Demo only: nothing was saved" as an HQ
  message. Reset demo, in the user menu, clears localStorage. The Demo data
  switch is locked on.
- **Response headers** cannot be set on GitHub Pages. Each page carries HQ's CSP
  as a meta tag, so the browser enforces it, and the headers HQ sent are saved
  for the connection panel, which says they were recorded at build time.

One more thing: what HQ says about the
reader's own connection (the connection panel, the dashboard's connection
reading, the user menu's Connection line) describes the laptop the demo was
rendered for, not whoever is reading it. `as_production()` sets HQ's
`SEVERINO_RENDERED_COPY`, a setting only code can turn on, and HQ's own
`partials/_example_connection.html` then marks each of those places as an example
connection, not the reader's. The wording lives in HQ; the demo only says it is a copy.

## What the checks hold (`demo/check.py`)

- Every address a page links or a script reads is a file in the demo.
- The main pages load with no console error and nothing from another site.
- A dismissal survives a reload until Reset demo clears it.
- No line ends in a separator with nothing after it.
- Every place that describes the reader's connection says it is an example.
- The first glance is healthy: no alarm on the dashboard, every control on the
  connection panel passing, and only the one expense missing its receipt.

## Publishing

`.github/workflows/demo.yml`, on each push to `main`: runs `browser:demo` and
uploads the folder it checked to GitHub Pages. The custom domain is set in the
repository's Pages settings. `demo/` is not copied into the Docker image.

## Files

| Path | What it is |
| --- | --- |
| `build.py` | The crawl (a test, so it gets its own database). |
| `demo.js` | The script the demo adds to each page. |
| `demo-theme.js` | Runs first: the chosen theme, and the saved copy of an address with a query. |
| `check.py` | The checks above; plain unittest, no Django. |
