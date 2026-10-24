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
| `mise run docs:screenshot` | Builds the showcase and retakes the README's pictures from it in WebKit, so they never show a real HQ. |

## Three seeds

| Seed | Built with | What it shows |
| --- | --- | --- |
| Showcase (default) | the example estate | A small homelab that has run for months: every reading present and current, every service set up, every expense with its receipt. Advanced HQ Posture is on. |
| Standard | `standard` | The same homelab with Advanced HQ Posture off. |
| Empty | `empty` | A fresh install: one signed-in user and nothing else. What someone sees the first time. |

## Two estates, two purposes

| Estate | Where | What it is for |
| --- | --- | --- |
| The example estate | `hq/platform/core/example_estate/` | The one homelab HQ is shown with: this demo, a dev database (`manage.py seed showcase`, every set named in `hq/platform/core/seeds.py`), the rendered-page and export tests, and the layout gate's realistic pass, which reads every page the navigation reaches. The checks and the demo read the same data. |
| The stress estate | `build_estate()` in `hq/platform/core/tests/test_browser_fixtures.py` | Long names, many rows, every state at once: it breaks things on purpose so the layout gate tests layouts at their worst. The demo never uses it. |

The example estate lives in `hq/`, not here, because `seed` and the suite
run inside the image, which `demo/` is not copied into. `seed` refuses a
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
  a page of a list, a Map node's details) are answered by HQ's server. The
  build saves each one a page links, each choice a filter offers (a checkbox,
  a select's options), the first six pages of a list, each address a script
  reads, and a search for each word of the pages' names, under `__q/` and
  `__fragments__/`; `demo-theme.js` opens the saved copy before the page
  paints. Sorts are not saved. On a page whose query has no copy, `demo.js`
  says which kind it was ("Sorting isn't saved in the demo"). Each page
  carries the policy HQ sends for its own route, so the API reference loads.
- **Forms** have nowhere to post. Dismissals are kept in the browser's
  localStorage; any other form shows "Demo only: nothing was saved" as an HQ
  message, and a post a script sends (a page asking for its readings as it
  opens) is answered with nothing changed. Reset demo, in the user menu,
  clears localStorage. The Demo data switch is locked on.
- **An address with no file** is answered by GitHub Pages with `404.html`,
  which is HQ's own not-found page.
- **Response headers** cannot be set on GitHub Pages. Each page carries HQ's CSP
  as a meta tag, so the browser enforces it, and the headers HQ sent are saved
  for the connection panel, which says they were recorded at build time.

What HQ says about the reader's own connection (the connection panel, the
dashboard's connection reading, the user menu's Connection line) describes the
laptop the demo was rendered for. `as_production()` sets HQ's
`SEVERINO_RENDERED_COPY`, a setting only code can turn on, and HQ's own
`partials/_example_connection.html` then marks each of those places as an
example connection. The wording lives in HQ; the demo only says it is a copy.

The same setting puts About this demo at the foot of every page, after its
content, a control that opens a dialog drawn by `partials/_demo_about.html`:
what the demo is, what to try, what doesn't save, links into this
repository, and the day the pages were saved. Without script the control is
a link to this README. The API reference names the example HQ as its server,
so Scalar does not offer GitHub Pages as one.

## What the checks hold (`demo/check.py`)

- Every address a page links or a script reads is a file in the demo.
- Next and Prev under a list, filtered or not, open a saved copy, up to the
  sixth page.
- Every page that draws a class or script hook no earlier page drew (so
  each template is loaded at least once), and the main pages, load in WebKit
  with no console error and nothing from another site. On the main pages
  Reset demo is in the user menu and the Demo data switch is locked on.
- Every page is drawn in the reader's color scheme.
- Every script on every page is a same-origin file its policy runs: a static
  page carries HQ's policy without the nonce and `'strict-dynamic'`, since a
  nonce saved into HTML is the same on every visit, and no tag carries one.
- The first glance is healthy: no alarm on the dashboard, every control on
  the connection panel passing, and only the three things left to do on
  purpose (an image update, a certificate to renew and an agent's request
  held for approval), each offering a way to do it.
- Every expense has its receipt.
- Every place that describes the reader's connection says it is an example.
- No line ends in a separator with nothing after it.
- The agents page shows an agent seen recently and a refusal counted, and the
  queue holds the change it asked for.
- The dashboard keeps its readings once it settles, and no reading's caption
  runs past it at 402 and 1360px.
- A machine's addresses read as one list that fits.
- A dismissal survives a reload until Reset demo clears it.
- About this demo follows the page's content at 390, 402, 820 and 1360px,
  is drawn over no control at any scroll stop on the dashboard, a list and
  the queue, and opens the dialog by keyboard, which Escape closes. Each link in the dialog names a file in this
  repository, and the dialog says the day the pages were saved, as HQ writes a
  day.
- `404.html` is HQ's not-found page, and the API document names a server.

HQ's own suite holds the other side: a running HQ with demo data on draws
the plain mark and no About control or dialog
(`hq/platform/application/tests/test_demo.py`).

## Publishing

`.github/workflows/demo.yml`, on each push to `main`, every Monday (so the
saved date and every age on the pages stay current while nothing lands), and
by hand (`workflow_dispatch`): runs `browser:demo` and uploads the folder it
checked to GitHub Pages. The custom domain is set in the
repository's Pages settings. `demo/` is not copied into the Docker image.

## Files

| Path | What it is |
| --- | --- |
| `build.py` | The crawl (a test, so it gets its own database). |
| `demo.js` | The script the demo adds to each page. |
| `demo-theme.js` | Runs first: the chosen theme, and the saved copy of an address with a query. |
| `check.py` | The checks above; plain unittest, no Django. |
