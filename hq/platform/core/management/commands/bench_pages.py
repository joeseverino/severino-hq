"""Time every page over a populated scratch database.

    DJANGO_STATIC_ROOT=/tmp/hq-static python manage.py bench_pages

Builds Django's test database (never the real one), fills it with
``hq.platform.core.bench.seed``, then requests each page as a signed-in
operator through the whole middleware stack. Reports median and p95 wall time,
the query count, how many of those queries repeat one already made in the same
request, how many derivations the request ran, and the response size. The
timed requests follow three others, so a page is measured as it is served
between two changes of what it derives from.

The pages are read from the URL configuration: every route that answers a GET.
A route that takes arguments is requested with the seeded record ``SAMPLES``
names for it; one with none listed is reported as not benched, so a new detail
page shows up here until it is given a sample.

Actions are measured the same way: every route that answers a POST is one, and
``ACTIONS`` names what each is posted. An action is posted as the page's script
posts it, inside a transaction that is rolled back, so the estate every later
round sees is the seeded one. What an action would do outside the database is
held: the controller's doorbell is not rung and a job's work is recorded but
not run, so the time is the request's own. A route with no entry is reported
as not exercised, with ``UNSAFE``'s reason where there is one.
"""

from __future__ import annotations

import functools
import json
import re
import statistics
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext, setup_databases, teardown_databases
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from hq.platform.application.derivations import counting
from hq.platform.application.resources import describe_resources
from hq.platform.application.security import host_capabilities
from hq.platform.core.bench import ZONES, Seeded, seed

# Routes that are not a page an operator loads. Named, with the reason.
SKIPPED = {
    "health_live": "probe",
    "health_ready": "probe",
    "csp_report": "browser report sink",
    "speculation_rules": "the browser's rule document",
    "login": "signed out",
    "logout": "signed out",
    "oidc_authentication_callback": "identity provider handshake",
    "oidc_authentication_init": "identity provider handshake",
    "oidc_logout": "identity provider handshake",
    "avatar": "image bytes",
    "receipts:file": "file bytes, none are stored by the seed",
    "control_plane:certificate_download": "file bytes",
    "control_plane:upload_certificate": "the seed uploads no certificate",
    "contacts:detail": "read from Cloudflare D1, not this database",
    "hq_api:execute": "write",
    "hq_api:resource-list": "expanded per resource below",
    "hq_api:resource-detail": "expanded per resource below",
}

# The seeded record each argument-taking route is requested with.
SAMPLES: dict[str, Callable[[Seeded], dict[str, Any]]] = {
    "projects:detail": lambda s: {"slug": s.project.slug},
    "projects:edit": lambda s: {"slug": s.project.slug},
    "projects:delete": lambda s: {"slug": s.project.slug},
    "docs_index:detail": lambda s: {"doc_id": s.documentation.doc_id},
    "docs_index:edit": lambda s: {"doc_id": s.documentation.doc_id},
    "docs_index:delete": lambda s: {"doc_id": s.documentation.doc_id},
    "content:detail": lambda s: {"slug": s.content.slug},
    "content:edit": lambda s: {"slug": s.content.slug},
    "content:delete": lambda s: {"slug": s.content.slug},
    "expenses:detail": lambda s: {"pk": s.expense.pk},
    "expenses:edit": lambda s: {"pk": s.expense.pk},
    "expenses:delete": lambda s: {"pk": s.expense.pk},
    "receipts:detail": lambda s: {"pk": s.receipt.pk},
    "receipts:edit": lambda s: {"pk": s.receipt.pk},
    "receipts:match": lambda s: {"pk": s.receipt.pk},
    "receipts:delete": lambda s: {"pk": s.receipt.pk},
    "assets:detail": lambda s: {"slug": s.asset.slug},
    "assets:edit": lambda s: {"slug": s.asset.slug},
    "assets:delete": lambda s: {"slug": s.asset.slug},
    "calendar:entry": lambda s: {"uid": s.entry.uid},
    "calendar:entry_edit": lambda s: {"uid": s.entry.uid},
    "calendar:entry_delete": lambda s: {"uid": s.entry.uid},
    "zones:detail": lambda s: {"zone": ZONES[0]},
    "zones:mail": lambda s: {"zone": ZONES[0]},
    "control_plane:machine": lambda s: {"name": "lab-1"},
    "control_plane:service": lambda s: {"hostname": s.resource.spec["domain_names"][0]},
    "control_plane:detail": lambda s: {"key": s.resource.key},
    "control_plane:edit": lambda s: {"key": s.resource.key},
    "control_plane:remove": lambda s: {"key": s.resource.key},
    "control_plane:report_download": lambda s: {"key": s.resource.key},
    "jobs:status": lambda s: {"pk": s.job.pk},
    "core:audit_detail": lambda s: {"pk": s.audit.pk},
    "core:approval_entry": lambda s: {"approval_id": s.approval.pk},
    "command": lambda s: {"name": "expense.create"},
}

# The same page asked a narrower question: searched, filtered, sorted, paged.
VARIANTS: dict[str, tuple[str, ...]] = {
    "search": ("q=example", "q=purchase"),
    "expenses:list": ("q=hosting", "category=hosting&year=2025", "sort=vendor", "page=40", "no_receipts=1"),
    "receipts:list": ("q=hosting", "page=30"),
    "assets:list": ("q=asset", "sort=item_name"),
    "core:audit_list": ("q=expense", "page=50"),
    "control_plane:list": ("q=app1",),
    "control_plane:topology_node": ("id=machine:lab-1",),
}


@dataclass(frozen=True)
class Action:
    """What one action is posted: its route arguments, its form, and what must
    exist first (made inside the same rolled-back transaction)."""

    kwargs: Callable[[Seeded], dict[str, Any]] = lambda seeded: {}
    data: Callable[[Seeded], dict[str, Any]] = lambda seeded: {}
    prepare: Callable[[Seeded], None] = lambda seeded: None
    headers: dict[str, str] = field(default_factory=lambda: {"X-Requested-With": "XMLHttpRequest"})


def _link_github(seeded: Seeded) -> None:
    from hq.platform.core.models import LinkedAccount

    LinkedAccount.objects.create(user=seeded.user, provider="github", login="example-user")


# The actions exercised: each changes only the scratch database.
ACTIONS: dict[str, Action] = {
    "watching_refresh": Action(prepare=_link_github),
    "projects:refresh": Action(kwargs=lambda s: {"slug": s.project.slug}),
    "control_plane:read_now": Action(data=lambda s: {"kind": s.resource.kind}),
    "control_plane:visit": Action(data=lambda s: {"subject": "machine", "name": "lab-1"}),
    "dashboard_glance": Action(),
}

# Actions that cannot be posted against a scratch database, and why.
UNSAFE = {
    "logout": "ends the bench's own session",
    "csp_report": "browser report sink",
    "hq_api:execute": "runs any capability; each is its own action",
    "contacts:set_status": "writes to Cloudflare D1, not this database",
    "contacts:delete": "writes to Cloudflare D1, not this database",
    "contacts:detail": "writes to Cloudflare D1, not this database",
    "control_plane:tools": "asks a public resolver and registry",
    "docs_index:import": "takes an uploaded manifest file",
    "control_plane:upload_certificate": "takes an uploaded certificate file",
    "receipts:create": "takes an uploaded file",
    "tool_public_address": "asks a public resolver and registry",
    "oidc_logout": "identity provider handshake",
}


@dataclass
class Result:
    page: str
    url: str
    status: int
    median_ms: float
    p95_ms: float
    queries: int
    repeated: int
    kilobytes: float
    # Derivations the measured request ran, rather than being served a stored answer.
    derived: int = 0


def _routes(patterns=None, prefix: str = "") -> Iterator[tuple[str, URLPattern]]:
    for pattern in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace == "djdt":
                continue
            space = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
            yield from _routes(pattern.url_patterns, space)
        elif pattern.name:
            yield f"{prefix}{pattern.name}", pattern


def _answers_get(pattern: URLPattern) -> bool:
    view = getattr(pattern.callback, "view_class", None)
    return view is None or hasattr(view, "get")


def _pages(seeded: Seeded) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(name, url) for every benched page, and (name, reason) for the rest."""

    pages: list[tuple[str, str]] = []
    left: list[tuple[str, str]] = []
    for name, pattern in _routes():
        if name in SKIPPED:
            left.append((name, SKIPPED[name]))
        elif not _answers_get(pattern):
            left.append((name, "write"))
        elif pattern.pattern.converters and name not in SAMPLES:
            left.append((name, "no sample in SAMPLES"))
        else:
            url = reverse(name, kwargs=SAMPLES[name](seeded) if name in SAMPLES else None)
            pages.append((name, url))
            pages.extend((f"{name}?{query}", f"{url}?{query}") for query in VARIANTS.get(name, ()))
    return pages, left


def _answers_post(name: str, pattern: URLPattern) -> bool:
    """Whether a route takes a POST. A class says so; a function view does not,
    so one is an action only where ``ACTIONS`` or ``UNSAFE`` names it."""

    view = getattr(pattern.callback, "view_class", None)
    if view is None:
        return name in ACTIONS or name in UNSAFE
    return "post" in view.http_method_names and hasattr(view, "post")


def _actions(seeded: Seeded) -> tuple[list[tuple[str, str, Action]], list[tuple[str, str]]]:
    """(name, url, what to post) for every exercised action, and (name, reason)
    for every other route that answers a POST."""

    exercised: list[tuple[str, str, Action]] = []
    left: list[tuple[str, str]] = []
    for name, pattern in _routes():
        if not _answers_post(name, pattern):
            continue
        action = ACTIONS.get(name)
        if action is None:
            left.append((name, UNSAFE.get(name, "no entry in ACTIONS")))
        else:
            exercised.append((name, reverse(name, kwargs=action.kwargs(seeded) or None), action))
    return exercised, left


@contextmanager
def _held_outside() -> Iterator[None]:
    """Hold what an action does outside the database: the doorbell stays
    unrung and a job's work is recorded, not run."""

    from hq.domains.jobs.testing import held_jobs

    with mock.patch("hq.platform.application.cadence._touch"), held_jobs():
        yield


def _post(client: Client, url: str, action: Action, seeded: Seeded):
    """One post of an action over the seeded estate, undone afterwards."""

    with transaction.atomic():
        action.prepare(seeded)
        with CaptureQueriesContext(connection) as captured:
            started = time.perf_counter()
            response = client.post(url, action.data(seeded), headers=action.headers)
            size = len(response.getvalue())
            took = (time.perf_counter() - started) * 1000
        transaction.set_rollback(True)
    return response, took, size, [query["sql"] for query in captured.captured_queries]


def _measure_action(
    client: Client, name: str, url: str, action: Action, seeded: Seeded, rounds: int
) -> tuple[Result, list[str]]:
    for _ in range(3):
        _post(client, url, action, seeded)
    times = []
    for _ in range(rounds):
        response, took, size, statements = _post(client, url, action, seeded)
        times.append(took)
    times.sort()
    return (
        Result(
            page=f"POST {name}",
            url=url,
            status=response.status_code,
            median_ms=round(statistics.median(times), 2),
            p95_ms=round(times[max(0, round(len(times) * 0.95) - 1)], 2),
            queries=len(statements),
            repeated=len(statements) - len(set(statements)),
            kilobytes=round(size / 1024, 1),
        ),
        statements,
    )


def _api_lists() -> list[tuple[str, str]]:
    return [
        (f"hq_api:resource-list {spec['name']}", reverse("hq_api:resource-list", args=[spec["name"]]))
        for spec in describe_resources()["resources"]
        if spec["operations"]["list"]
    ]


@contextmanager
def _machine_client() -> Iterator[dict[str, str]]:
    """Headers the API accepts as a client granted everything an operator holds.

    The token is not verified: a signature check needs an identity provider,
    and what it costs is not the page's doing.
    """

    claims = {"client_id": "bench", "scope": " ".join(sorted(map(str, host_capabilities())))}
    with (
        # Not ``setup_test_environment``: it instruments every template render
        # to keep a copy of its context, which a served page never pays for.
        override_settings(SEVERINO_API_RESOURCE="https://hq.example.com", ALLOWED_HOSTS=["testserver"]),
        mock.patch("hq.platform.api.views.verify", return_value=claims),
    ):
        yield {"Authorization": "Bearer bench"}


# A region that names the part of its page it is: `data-fragment` (its
# address; empty for the page it is on), then `data-fragment-name`.
_PART = re.compile(
    r'\bdata-fragment(?:="([^"]*)")?(?=\s)[^>]*?\bdata-fragment-name="(\w+)"'
)


def _asked_again(client: Client, page: str, url: str) -> list[tuple[str, str, dict[str, str]]]:
    """The narrower questions a browser asks of ``url`` after loading it.

    Each part the page says it answers by name, asked for alone, and the page
    asked about with the validator it handed out. Read from the response, so a
    page that gains a part or a validator is measured without being listed.
    """

    response = client.get(url)
    if response.status_code != 200:
        return []
    body = response.getvalue().decode(errors="replace")
    path = url.split("?")[0]
    again = [
        (f"{page} #{name}", url, {"X-Fragment": name})
        for name in sorted({name for address, name in _PART.findall(body) if address in ("", path)})
    ]
    if "ETag" in response:
        again.append((f"{page}, validator presented", url, {"If-None-Match": response["ETag"]}))
    return again


def _measure(
    client: Client, page: str, url: str, rounds: int, headers: dict[str, str] | None = None
) -> tuple[Result, list[str]]:
    get = functools.partial(client.get, url, headers=headers)
    for _ in range(3):
        response = get()
    times = []
    for _ in range(rounds):
        started = time.perf_counter()
        response = get()
        # A streamed body is part of what the page costs.
        size = len(response.getvalue())
        times.append((time.perf_counter() - started) * 1000)
    with counting() as (ran, _served), CaptureQueriesContext(connection) as captured:
        get().getvalue()
    statements = [query["sql"] for query in captured.captured_queries]
    times.sort()
    return (
        Result(
            page=page,
            url=url,
            status=response.status_code,
            median_ms=round(statistics.median(times), 2),
            p95_ms=round(times[max(0, round(len(times) * 0.95) - 1)], 2),
            queries=len(statements),
            repeated=len(statements) - len(set(statements)),
            kilobytes=round(size / 1024, 1),
            derived=sum(ran.values()),
        ),
        statements,
    )


class Command(BaseCommand):
    help = "Time every page and action, with its query count, over a populated scratch database."

    def add_arguments(self, parser):
        parser.add_argument("--scale", type=float, default=1.0, help="Multiplies the seeded counts.")
        parser.add_argument("--rounds", type=int, default=30, help="Timed requests per page.")
        parser.add_argument("--only", default="", help="Bench only pages and actions whose name contains this.")
        parser.add_argument("--sql", action="store_true", help="Print each benched page's queries.")
        parser.add_argument("--json", default="", help="Also write the results to this file.")

    def handle(self, *args, **options):
        if not settings.STATIC_LIVE:
            # Uncollected, hashed storage hashes each asset on every request.
            call_command("collectstatic", interactive=False, verbosity=0)
        databases = setup_databases(verbosity=0, interactive=False)
        try:
            results, left, unexercised = self._run(options)
        finally:
            teardown_databases(databases, verbosity=0)
        self._report(results, left, unexercised)
        if options["json"]:
            with open(options["json"], "w", encoding="utf-8") as out:
                json.dump([asdict(result) for result in results], out, indent=2)

    def _run(self, options) -> tuple[list[Result], list[tuple[str, str]], list[tuple[str, str]]]:
        seeded = seed(options["scale"])
        pages, left = _pages(seeded)
        pages += _api_lists()
        actions, unexercised = _actions(seeded)
        with _machine_client() as headers:
            client = Client(headers=headers, raise_request_exception=False)
            client.force_login(seeded.user)
            wanted = [(page, url, None) for page, url in pages if options["only"] in page]
            asked = [
                again
                for page, url, _ in wanted
                for again in _asked_again(client, page, url)
            ]
            results = [
                self._said(options, *_measure(client, page, url, options["rounds"], headers))
                for page, url, headers in wanted + asked
            ]
            with _held_outside():
                results += [
                    self._said(options, *_measure_action(client, name, url, action, seeded, options["rounds"]))
                    for name, url, action in actions
                    if options["only"] in f"POST {name}"
                ]
        return results, left, unexercised

    def _said(self, options, result: Result, statements: list[str]) -> Result:
        """The result, after printing its queries where they were asked for."""

        if options["sql"]:
            self.stdout.write(f"\n-- {result.page}: {result.queries} queries")
            for statement, times in Counter(statements).most_common():
                self.stdout.write(f"{times:4d}x {statement[:400]}")
        return result

    def _report(
        self, results: list[Result], left: list[tuple[str, str]], unexercised: list[tuple[str, str]]
    ) -> None:
        self.stdout.write(
            f"\n{'page':58s} {'status':>6s} {'median':>9s} {'p95':>9s} {'queries':>7s} {'repeat':>6s} {'derive':>6s} {'KB':>8s}"
        )
        for result in sorted(results, key=lambda result: -result.median_ms):
            self.stdout.write(
                f"{result.page[:58]:58s} {result.status:6d} {result.median_ms:7.2f}ms {result.p95_ms:7.2f}ms "
                f"{result.queries:7d} {result.repeated:6d} {result.derived:6d} {result.kilobytes:8.1f}"
            )
        self.stdout.write("\nNot benched:")
        for name, reason in left:
            if reason != "write":
                self.stdout.write(f"  {name}: {reason}")
        self.stdout.write("\nActions not exercised:")
        for name, reason in unexercised:
            self.stdout.write(f"  {name}: {reason}")
