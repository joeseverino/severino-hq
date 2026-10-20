"""Builds the public demo: HQ's pages for the example data, saved as a static site.

Runs as a test (``mise run demo:build``) so it gets a throwaway database. It
seeds the showcase (``hq/platform/core/example_estate``), signs in with demo data on, asks for
each page the way a production request arrives, follows every link from the
dashboard and saves each page to ``HQ_DEMO_OUT``. GET requests change nothing
in HQ, so crawling is safe.

A static host can't do four things HQ relies on:

- Partial page loads send an ``X-Fragment`` header. The build saves each
  response under ``__fragments__/`` and ``demo.js`` serves it from there.
- An address with a query (a filter, a day, Show all, a search) is answered by
  the server. The build saves each one a page links, a filter offers or a
  script reads under ``__q/``, listed in ``__fragments__/variants.json``;
  ``demo-theme.js`` opens the saved copy, and ``demo.js`` says so when there
  is none.
- Forms have nowhere to post. ``demo.js`` stores dismissals in localStorage and
  shows "Demo only: nothing was saved" for everything else.
- It can't send HQ's response headers. Each page carries HQ's CSP as a meta
  tag, and the headers HQ sent are saved for the connection panel to show.

``demo/`` is not copied into the Docker image.
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from functools import cache
from html import escape, unescape
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
lazy from django.contrib.staticfiles.storage import staticfiles_storage

from hq.platform.core.example_estate import REQUEST, SSO, as_production, build_showcase
from hq.platform.core.example_estate.names import HQ, HQ_SOURCE
from hq.platform.core.middleware import DEMO_SESSION_KEY

OUT = Path(os.environ.get("HQ_DEMO_OUT") or Path(settings.BASE_DIR) / "var" / "demo")
# Pages that need sign-in, an API token or an outside provider.
_SKIPPED = ("/accounts/", "/oidc/", "/api/v2/", "/static/", "/csp-report/", "/admin/", "/media/", "/_internal-media/")
# Stops a link loop from running forever.
_MOST_PAGES = 800
# Links, and addresses a script reads from the page (data-*-url).
_HREF = re.compile(r'(?:href|data-[\w-]+-url)="(/[^"#?]*)')
_REGION = re.compile(r'<[^>]*\sdata-fragment="([^"]+)"[^>]*>')
_NAME = re.compile(r'\sdata-fragment-name="([^"]*)"')
# Inserted right after the charset tag, before HQ's own scripts.
_INJECT = '<script src="/static/js/demo-theme.js"></script><script defer src="/static/js/demo.js"></script>'
_CHARSET = '<meta charset="utf-8">'
# Directives a meta tag can't carry; browsers ignore them there and say so.
_HEADER_ONLY = ("frame-ancestors", "report-uri", "report-to", "sandbox")
# Links with a query, a filter's on-or-off choice, and addresses a script
# reads with one (a Map node's details).
_QUERY_HREF = re.compile(r'href="(/[^"#?]*\?[^"#]*)"')
_TOGGLE = re.compile(r'<label class="table-toggle"><input type="checkbox" name="([^"]+)" value="([^"]*)"')
_READ_WITH_QUERY = re.compile(r'\sdata-(?!fragment)[\w-]+="(/[^"#?]*\?[^"#]*)"')
# A sort or a page of a list is the same rows again: not saved.
_SAME_ROWS = {"sort", "page"}
# The most query answers kept, so a page of many links cannot fill the demo.
_MOST_VARIANTS = 400
# Searches answered: each word of a page's name, up to this many.
_MOST_SEARCHES = 200
_TITLE = re.compile(r"<title>(.*?)(?: · [^<]*)?</title>", re.DOTALL)
_WORD = re.compile(r"[a-z0-9][a-z0-9.-]{2,}")
_FORMS = {"Add", "Delete", "Edit", "Import", "Look", "New", "Publish", "Refresh", "Remove", "Sync", "Upload", "What"}
_FILLER = {"and", "but", "the", "you", "your", "for", "off", "has", "why", "every", "actually", "itself", "finds"}
# What HQ answers a search that finds nothing, for a word the demo has no answer for.
_NOTHING = "zz-nothing"
# Headers that describe one response, not HQ's policy.
_PER_RESPONSE = {"set-cookie", "content-length", "content-type", "date", "vary", "expires", "cache-control"}


def _wanted(path: str) -> bool:
    return path.startswith("/") and not path.startswith(_SKIPPED)


def query_key(path: str, query: str) -> str:
    """One address however its query was written: its pairs in order, empty
    ones left out. ``demo-theme.js`` keys the same way."""

    pairs = sorted((name, value) for name, value in parse_qsl(query, keep_blank_values=True) if value)
    return f"{path}?{urlencode(pairs)}" if pairs else path


def _variant_worth_saving(address: str) -> bool:
    parts = urlsplit(address)
    names = {name for name, _value in parse_qsl(parts.query)}
    return _wanted(parts.path) and bool(names) and not names & _SAME_ROWS


# Icons a browser asks for at the site root, by their own names.
_ROOT_ICONS = ("favicon.ico", "apple-touch-icon.png")
# Redirect page, since a static host can't send a redirect.
# It says it is drawn in either scheme, so a dark reader is not shown a white
# page for the moment before it moves on.
_MOVED = (
    '<!doctype html><meta charset="utf-8"><meta name="color-scheme" content="light dark">'
    '<meta http-equiv="refresh" content="0; url={target}"><link rel="canonical" href="{target}">'
)


def _is_page(response) -> bool:
    kind = response.get("Content-Type", "")
    return "text/html" in kind or (not kind and response.content.lstrip()[:15].lower().startswith(b"<!doctype html"))


def _write(path: str, content: bytes, *, page: bool = True) -> None:
    """Save a page as ``path/index.html``, or a file under its own name."""

    relative = path.strip("/")
    target = OUT / relative / "index.html" if page or path.endswith("/") or not relative else OUT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)


class _Browser(Client):
    """Asks over HTTPS, as every request to HQ in production does."""

    def get(self, path, *args, **kwargs):
        kwargs.setdefault("secure", True)
        return super().get(path, *args, **kwargs)


def _save_download(path: str, response) -> None:
    """A CSV, JSON or receipt file, under a name a static host serves as that type.

    An address ending in ``/`` would be served as a page, so the file is saved
    beside it with its extension and the address redirects to it.
    """

    content = b"".join(response.streaming_content) if response.streaming else response.content
    if not path.endswith("/"):
        _write(path, content, page=False)
        return
    kind = response.get("Content-Type", "").split(";")[0]
    target = path.rstrip("/") + (mimetypes.guess_extension(kind) or "")
    _write(target, content, page=False)
    _write(path, _MOVED.format(target=target).encode())


@cache
def _repository() -> str:
    """The repository this demo is built from: GitHub Actions says so, and a
    checkout's origin does locally. Blank when neither does."""

    if repository := os.environ.get("GITHUB_REPOSITORY", ""):
        return f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repository}"
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True, cwd=settings.BASE_DIR
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return ""
    found = re.match(r"(?:git@github\.com:|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?$", origin)
    return f"https://github.com/{found.group(1)}" if found else ""


def _real_docs(page: str) -> str:
    """Links into HQ's docs point at the real ones. The showcase's own HQ stays
    example data: its commit and image are made up, so only the docs move."""

    repository = _repository()
    return page.replace(f"{HQ_SOURCE}/blob/main/docs/", f"{repository}/blob/main/docs/") if repository else page


def _meta_policy(response) -> str:
    """HQ's CSP for this response, as a meta tag, so the browser enforces on
    each page the policy HQ sends for its route."""

    kept = [
        directive.strip()
        for directive in response.get("Content-Security-Policy", "").split(";")
        if directive.strip() and directive.split()[0] not in _HEADER_ONLY
    ]
    return f'<meta http-equiv="Content-Security-Policy" content="{escape("; ".join(kept))}">' if kept else ""


def _save_headers(response) -> None:
    """The headers HQ sent, for the connection panel to show."""

    sent = {name.lower(): value for name, value in response.headers.items() if name.lower() not in _PER_RESPONSE}
    # Set by the proxy in front of HQ, which the demo's HQ is shown behind.
    sent.setdefault("x-served-by", HQ)
    (OUT / "__fragments__").mkdir(exist_ok=True)
    (OUT / "__fragments__" / "headers.json").write_text(json.dumps(sent, indent=1), encoding="utf-8")


@contextmanager
def _hashed_static():
    """Static files collected and named by content, as the image build does it.

    Pages then link ``app.3f2a1b9c.css``, so a new build is never read from a
    stale cache of the last one.
    """

    root = Path(tempfile.mkdtemp())
    storages = {**settings.STORAGES, "staticfiles": {"BACKEND": "hq.platform.core.static.HashedStaticStorage"}}
    with override_settings(STATIC_ROOT=root, STORAGES=storages):
        call_command("collectstatic", interactive=False, verbosity=0)
        yield root


def _fresh_install():
    """What someone sees after installing HQ: one signed-in user, nothing else."""

    return get_user_model().objects.create_user(username="example", email="example@example.com", first_name="Example")


# The showcase by default, under Advanced HQ Posture. HQ_DEMO_SEED=standard is
# the same estate without it, and empty a fresh install.
_SEEDS = {
    "showcase": (build_showcase, {"SEVERINO_POSTURE": "advanced"}),
    "standard": (build_showcase, {}),
    "empty": (_fresh_install, {}),
}


def _inject(page: str, response, *, address: str = "") -> bytes:
    """A page as the demo serves it: HQ's policy for its route, the address a
    saved copy stands for, and the demo's scripts, after the charset."""

    shown = f'<meta name="hq-demo-address" content="{escape(address)}">' if address else ""
    return _real_docs(page).replace(_CHARSET, _CHARSET + _meta_policy(response) + shown + _INJECT, 1).encode()


class _Offered:
    """What the crawled pages offer that has a query: links (Show all, a day,
    an event), a filter's on-or-off choices, the addresses scripts read, and
    the words of each page's name, to search for."""

    def __init__(self) -> None:
        self.variants: set[str] = set()
        self.reads: set[str] = set()
        self.words: set[str] = set()

    def read(self, path: str, page: str) -> None:
        self.variants.update(
            address for address in map(unescape, _QUERY_HREF.findall(page)) if _variant_worth_saving(address)
        )
        self.variants.update(f"{path}?{urlencode({name: unescape(value)})}" for name, value in _TOGGLE.findall(page))
        self.reads.update(unescape(address) for address in _READ_WITH_QUERY.findall(page))
        title = _TITLE.search(page)
        name = unescape(title.group(1)) if title else ""
        # A form's name is what it does ("Edit app-host"); what it is about is named on its own page.
        if name and name.split()[0] not in _FORMS:
            self.words.update(word for word in _WORD.findall(name.lower()) if word not in _FILLER)


class DemoBuild(TestCase):
    """A test only so it gets its own database."""

    def test_build(self):
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        seed, seeded_settings = _SEEDS[os.environ.get("HQ_DEMO_SEED") or "showcase"]
        with as_production(), override_settings(**seeded_settings), _hashed_static() as collected:
            client = _Browser(**REQUEST)
            client.force_login(seed(), backend=SSO)
            # Demo data on for every page; demo.js keeps the switch from turning it off.
            session = client.session
            session[DEMO_SESSION_KEY] = True
            session.save()
            self._crawl(client, collected)

    def _crawl(self, client: Client, collected: Path) -> None:
        fragments: dict[str, str] = {}
        offered = _Offered()
        seen: set[str] = set()
        self._walk(client, ["/"], seen, fragments, offered)
        variants = self._save_variants(client, offered, fragments)
        # The saved copies and answers link pages no page did (a search's
        # results, an audit entry on a filtered day): those are saved too.
        found = [*(OUT / "__q").rglob("*.html"), *(OUT / "__fragments__").glob("*.html")]
        self._walk(
            client,
            [unescape(href) for file in found for href in _HREF.findall(file.read_text(encoding="utf-8"))],
            seen,
            fragments,
            offered,
        )
        (OUT / "__fragments__").mkdir(exist_ok=True)
        (OUT / "__fragments__" / "index.json").write_text(json.dumps(fragments, indent=1), encoding="utf-8")
        (OUT / "__fragments__" / "variants.json").write_text(json.dumps(variants, indent=1), encoding="utf-8")
        self._copy_static(collected)
        self._copy_root(client)
        self.assertGreater(len(seen), 20)

    def _walk(self, client: Client, queue: list[str], seen: set[str], fragments: dict, offered: _Offered) -> None:
        """Save every page reachable from ``queue`` that is not saved yet."""

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
                _save_download(path, response)
                continue
            if not (OUT / "__fragments__" / "headers.json").exists():
                _save_headers(response)
            page = response.content.decode()
            for fragment in self._save_fragments(client, page, fragments):
                queue.extend(unescape(href) for href in _HREF.findall(fragment))
                offered.read(path, fragment)
            offered.read(path, page)
            _write(path, _inject(page, response))
            queue.extend(unescape(href) for href in _HREF.findall(page))

    def _save_fragments(self, client: Client, page: str, fragments: dict[str, str]) -> list[str]:
        """Save each partial-load response, and the regions inside those, and
        return them so the crawl follows their links."""

        saved: list[str] = []
        pending = [page]
        while pending:
            for region in _REGION.finditer(pending.pop()):
                url = unescape(region.group(1))
                if not _wanted(urlsplit(url).path):
                    continue
                name_match = _NAME.search(region.group(0))
                name = unescape(name_match.group(1)) if name_match else ""
                answer = self._save_answer(client, url, name, fragments)
                if answer is not None:
                    saved.append(answer)
                    pending.append(answer)
        return saved

    def _save_answer(self, client: Client, url: str, name: str, fragments: dict[str, str]) -> str | None:
        """One partial-load response, kept under its address and part; None
        when it is kept already or does not answer."""

        parts = urlsplit(url)
        key = f"{query_key(parts.path, parts.query)}|{name}"
        if key in fragments:
            return None
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest", **({"HTTP_X_FRAGMENT": name} if name else {})}
        response = client.get(url, **headers)
        if response.status_code != 200:
            return None
        file = f"{len(fragments)}.html"
        (OUT / "__fragments__").mkdir(exist_ok=True)
        (OUT / "__fragments__" / file).write_text(_real_docs(response.content.decode()), encoding="utf-8")
        fragments[key] = f"/__fragments__/{file}"
        return response.content.decode()

    def _save_variants(self, client: Client, offered: _Offered, fragments: dict[str, str]) -> dict:
        """Every address with a query the pages offered, saved as a page under
        ``__q/``; what a script reads with a query, as an answer; and a search
        for each word of the pages' names. The list ``demo-theme.js`` reads."""

        searches = sorted(offered.words)[:_MOST_SEARCHES]
        asked = [
            *sorted(offered.variants)[:_MOST_VARIANTS],
            *(f"/search/?{urlencode({'q': word})}" for word in [*searches, _NOTHING]),
        ]
        variants: dict[str, str] = {}
        for address in asked:
            parts = urlsplit(address)
            key = query_key(parts.path, parts.query)
            response = client.get(address)
            if key in variants or response.status_code != 200 or not _is_page(response):
                continue
            target = f"/__q/{len(variants)}/"
            page = response.content.decode()
            self._save_fragments(client, page, fragments)
            _write(target, _inject(page, response, address=key))
            variants[key] = target
        for address in sorted(offered.reads):
            self._save_answer(client, address, "", fragments)
        for word in [*searches, _NOTHING]:
            self._save_answer(client, f"/search/?{urlencode({'q': word})}", "palette", fragments)
        return {"variants": variants, "searches": searches, "nothing": _NOTHING}

    def _copy_static(self, collected: Path) -> None:
        """HQ's collected static files, as production names them, plus demo.js.

        Compressed copies are left out: GitHub Pages compresses what it serves.
        So are originals a hashed copy stands in for, but the icons a browser
        asks for at the root by name.
        """

        # An original that has a hashed copy is never linked: pages name the copy.
        named = json.loads((collected / "staticfiles.json").read_text(encoding="utf-8"))["paths"]
        unlinked = {collected / original for original in named} - {collected / "img" / icon for icon in _ROOT_ICONS}
        for source in collected.rglob("*"):
            if source.is_file() and source.suffix not in {".gz", ".br"} and source not in unlinked:
                target = OUT / "static" / source.relative_to(collected)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
        for script in ("demo.js", "demo-theme.js"):
            shutil.copyfile(Path(__file__).with_name(script), OUT / "static" / "js" / script)

    def _copy_root(self, client: Client) -> None:
        """Copy the icons, the API document, the speculation rules and the liveness check."""

        # By the collected name: only content-named copies are kept.
        for icon in _ROOT_ICONS:
            shutil.copyfile(OUT / "static" / staticfiles_storage.stored_name(f"img/{icon}"), OUT / icon)
        # The API docs page loads this.
        document = client.get("/api/v2/openapi.json")
        if document.status_code == 200:
            _write("/api/v2/openapi.json", document.content, page=False)
        rules = client.get("/speculation-rules.json")
        if rules.status_code == 200:
            (OUT / "speculation-rules.json").write_bytes(rules.content)
        # The connection panel times a round trip to this.
        live = client.get("/health/live/")
        if live.status_code == 200:
            _write("/health/live/", live.content, page=False)
        # Stops GitHub Pages from processing the files.
        (OUT / ".nojekyll").write_text("", encoding="utf-8")
