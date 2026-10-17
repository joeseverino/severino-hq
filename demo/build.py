"""Builds the public demo: HQ's pages for the example data, saved as a static site.

Runs as a test (``mise run demo:build``) so it gets a throwaway database. It
loads the same example data as the browser gate, signs in, follows every link
from the dashboard and saves each page to ``HQ_DEMO_OUT``. GET requests change
nothing in HQ, so crawling is safe.

A static host can't do two things HQ relies on:

- Partial page loads send an ``X-Fragment`` header. The build saves each
  response under ``__fragments__/`` and ``demo.js`` serves it from there.
- Forms have nowhere to post. ``demo.js`` stores dismissals in localStorage and
  shows "Demo only: nothing was saved" for everything else.

``demo/`` is not copied into the Docker image.
"""

import json
import os
import re
import shutil
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.staticfiles import finders
from django.test import Client, TestCase

from hq.platform.application.reach import TAILNET
from hq.platform.core.tests.test_browser_fixtures import build_estate

OUT = Path(os.environ.get("HQ_DEMO_OUT") or Path(settings.BASE_DIR) / "var" / "demo")
# Pages that need sign-in, an API token or an outside provider.
_SKIPPED = ("/accounts/", "/oidc/", "/api/v2/", "/static/", "/csp-report/", "/admin/", "/media/", "/_internal-media/")
# Stops a link loop from running forever.
_MOST_PAGES = 800
# Requests come from a tailnet address, like a real reader's.
_READER = str(next(TAILNET[0].hosts()))
_HREF = re.compile(r'href="(/[^"#?]*)')
_REGION = re.compile(r'<[^>]*\sdata-fragment="([^"]+)"[^>]*>')
_NAME = re.compile(r'\sdata-fragment-name="([^"]*)"')
# Inserted right after the charset tag, before HQ's own scripts.
_INJECT = '<script defer src="/static/js/demo.js"></script><link rel="stylesheet" href="/static/css/demo.css">'


def _wanted(path: str) -> bool:
    return path.startswith("/") and not path.startswith(_SKIPPED)


# Redirect page, since a static host can't send a redirect.
_MOVED = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url={target}"><link rel="canonical" href="{target}">'


def _is_page(response) -> bool:
    kind = response.get("Content-Type", "")
    return "text/html" in kind or (not kind and response.content.lstrip()[:15].lower().startswith(b"<!doctype html"))


def _write(path: str, content: bytes, *, page: bool = True) -> None:
    """Save a page as ``path/index.html``, or a file under its own name."""

    relative = path.strip("/")
    target = OUT / relative / "index.html" if page or path.endswith("/") or not relative else OUT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


class DemoBuild(TestCase):
    """A test only so it gets its own database."""

    def test_build(self):
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        client = Client(REMOTE_ADDR=_READER)
        client.force_login(build_estate())

        fragments: dict[str, str] = {}
        queue, seen = ["/"], set()
        while queue and len(seen) < _MOST_PAGES:
            path = queue.pop(0)
            if path in seen or not _wanted(path):
                continue
            seen.add(path)
            response = client.get(path)
            if response.status_code in (301, 302) and _wanted(response["Location"]):
                target = urlsplit(response["Location"]).path
                _write(path, _MOVED.format(target=target).encode())
                queue.append(target)
                continue
            if response.status_code != 200:
                continue
            if not _is_page(response):
                # A download, such as a CSV or JSON export.
                _write(path, response.content, page=False)
                continue
            page = response.content.decode()
            for fragment in self._save_fragments(client, page, fragments):
                queue.extend(unescape(href) for href in _HREF.findall(fragment))
            _write(path, page.replace('<meta charset="utf-8">', '<meta charset="utf-8">' + _INJECT, 1).encode())
            queue.extend(unescape(href) for href in _HREF.findall(page))

        (OUT / "__fragments__").mkdir(exist_ok=True)
        (OUT / "__fragments__" / "index.json").write_text(json.dumps(fragments, indent=1), encoding="utf-8")
        self._copy_static()
        self._copy_root(client)
        self.assertGreater(len(seen), 20)

    def _save_fragments(self, client: Client, page: str, fragments: dict[str, str]) -> list[str]:
        """Save each partial-load response, and return them so the crawl follows their links."""

        saved: list[str] = []

        for tag in _REGION.findall(page):
            url = unescape(tag)
            if not _wanted(urlsplit(url).path):
                continue
            region = next((m for m in _REGION.finditer(page) if m.group(1) == tag), None)
            name_match = _NAME.search(region.group(0)) if region else None
            name = unescape(name_match.group(1)) if name_match else ""
            key = f"{url}|{name}"
            if key in fragments:
                continue
            headers = {"HTTP_X_FRAGMENT": name, "HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if name else {}
            response = client.get(url, **headers)
            if response.status_code != 200:
                continue
            file = f"{len(fragments)}.html"
            (OUT / "__fragments__").mkdir(exist_ok=True)
            (OUT / "__fragments__" / file).write_bytes(response.content)
            fragments[key] = f"/__fragments__/{file}"
            saved.append(response.content.decode())
        return saved

    def _copy_static(self) -> None:
        """Copy HQ's static files, plus demo.js and demo.css."""

        for name, folder in (("demo.js", "js"), ("demo.css", "css")):
            target = OUT / "static" / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(__file__).with_name(name), target)

        for finder in finders.get_finders():
            for relative, storage in finder.list([]):
                target = OUT / "static" / relative
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with storage.open(relative) as source:
                    target.write_bytes(source.read())

    def _copy_root(self, client: Client) -> None:
        """Copy the icons, the API document and the speculation rules to the site root."""

        for path, name in (("/favicon.ico", "img/favicon.ico"), ("/apple-touch-icon.png", "img/apple-touch-icon.png")):
            shutil.copyfile(OUT / "static" / name, OUT / path.lstrip("/"))
        # The API docs page loads this.
        document = client.get("/api/v2/openapi.json")
        if document.status_code == 200:
            _write("/api/v2/openapi.json", document.content, page=False)
        rules = client.get("/speculation-rules.json")
        if rules.status_code == 200:
            (OUT / "speculation-rules.json").write_bytes(rules.content)
        # Stops GitHub Pages from processing the files.
        (OUT / ".nojekyll").write_text("", encoding="utf-8")
