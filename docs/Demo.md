# Demo

The public demo is HQ's own pages, filled with the example data the browser
gate uses and saved as a static site. It uses the same templates, CSS, scripts
and font as HQ.

| Step | Command | What it does |
| --- | --- | --- |
| Build | `mise run demo:build` (`demo/build.py`) | Loads the example data into a throwaway database, signs in, follows every link from the dashboard and saves each page to `var/demo` with the static files. |
| Check | `mise run browser:demo` (`demo/check.py`) | Builds, then serves `var/demo` over plain HTTP and checks it in WebKit: every link points to a file that exists, pages load with no errors and nothing from another site, and a dismissal survives a reload until Reset demo clears it. Part of the `browser` gate, so every pull request runs it. |
| Publish | `.github/workflows/demo.yml` | On each push to `main`, runs the check and publishes the folder it checked to GitHub Pages. The custom domain is set in the repository's Pages settings. |

A static host can't do two things HQ relies on. Both are handled in `demo/`,
not in HQ's templates:

- Partial page loads send an `X-Fragment` header. The build saves each response
  under `__fragments__/` and `demo/demo.js` serves it from there.
- Forms have nowhere to post. Dismissals are saved in the browser's
  localStorage; any other form shows "Demo only: nothing was saved". Reset demo,
  in the user menu, clears localStorage.

`demo/` is not copied into the Docker image, so neither the image nor a
composition includes it. The demo shows the host only: extensions are private
packages, and HQ names none of them.
