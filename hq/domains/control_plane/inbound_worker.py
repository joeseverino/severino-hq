"""The inbound-events Worker's configuration, derived from the providers'
``INBOUND`` declarations, so the Worker verifies each provider's requests
and the operator subscribes each provider to exactly the events HQ takes.

``routes.json`` is the one place the Worker and its tests learn each
provider's route and the listener's socket, ``arrival.json`` the bridge
contract's ``InboundArrival``, the one shape the Worker sends, and
``wrangler.jsonc`` requires each route's secret and sends only those paths to
the Worker. Every other request is answered from ``public/``: HQ's page frame
rendered once, naming the providers and their events, and its not-found page.
The stylesheet, font and scripts those pages link are HQ's own ``static/``,
which ``worker:deploy`` copies beside them.
"""

import hashlib
import json
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string

from hq.platform.application.entity_links import provider_label
from hq.platform.application.pages import Page
from hq.platform.core import source

from .observations.cloudflare import ANSWERS_DATASET
from .provider_adapters import INBOUND
lazy from .bridge_contract import contract

# The Worker secret the listener's socket presents as its bearer: the
# Worker's own, never a provider's, so no INBOUND declaration names it.
SOCKET_TOKEN = "SOCKET_TOKEN"  # noqa: S105 - the Worker secret's name, never its value
# Where the listener opens its socket to the Worker.
SOCKET_PATH = "/socket"
# The directory the Worker's static pages are served from, beside wrangler.jsonc.
PUBLIC = "public"
# The Worker's static pages under it: its root, and what any path it does not answer is given.
ROOT, MISSING = "index.html", "404.html"

HEADER = """\
// The inbound-events Worker (src/index.ts). The account and the route's
// hostname come from the deploy's environment, so this public file names
// neither. Written by `mise run manage -- inbound_worker` from the providers'
// INBOUND declarations.
"""

# What the Worker is, whatever the providers declare.
SETTINGS = {
    "$schema": "node_modules/wrangler/config-schema.json",
    "name": "hq-events",
    "main": "src/index.ts",
    "compatibility_date": "2026-11-23",
    "workers_dev": False,
    # Each version's preview URL is public and names the account. Left unset,
    # a deploy keeps whatever the dashboard last set.
    "preview_urls": False,
    # wrangler reports neither its usage nor the Worker's package versions,
    # for every command run against this file.
    "send_metrics": False,
    "dependencies_instrumentation": {"enabled": False},
    # A runaway's ceiling, held well above what verifying and parsing the
    # largest body a provider sends takes, since GitHub never retries a
    # delivery the Worker drops.
    "limits": {"cpu_ms": 1000},
    "exports": {"Hub": {"type": "durable-object", "storage": "sqlite"}},
    "durable_objects": {"bindings": [{"name": "HUB", "class_name": "Hub"}]},
    # One data point per request the Worker answers, which the cloudflare.worker
    # reading counts by route and status.
    "analytics_engine_datasets": [{"binding": "ANSWERS", "dataset": ANSWERS_DATASET}],
    # Every request is logged, by the Worker's own line of its method, path
    # and status: invocation logs keep the request's headers, and Cloudflare
    # documents no redaction of them in Workers Logs. Issues groups the
    # Worker's exceptions and 5xx answers from those logs.
    "observability": {
        "enabled": True,
        "head_sampling_rate": 1,
        "logs": {"invocation_logs": False},
        "issues": {"enabled": True},
    },
}

# What the Worker never reads: HQ's readings for each event, which of them
# the events keep current, what the provider's app must hold, and how a
# subject is worded. Of each fact it reads the pointers alone.
OFF_ROUTE = {"events", "complete", "requires", "facts", "worded"}


def _plain(value: object) -> object:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else value


def routes() -> list[dict[str, object]]:
    """Each provider the Worker receives events from: its route, then the
    events HQ takes, which ``wire-github-app`` and the install guide name to
    subscribe to. A source that reports over the bridge alone has none."""

    return [
        {
            "provider": provider,
            **{field.name: _plain(getattr(spec, field.name)) for field in fields(spec) if field.name not in OFF_ROUTE},
            "events": list(spec.events),
            "facts": {
                event: {"values": dict(fact.values), "about": fact.about, "said_at": list(fact.said_at)}
                for event, fact in spec.facts.items()
            },
        }
        for provider, spec in INBOUND.items()
        if spec.path
    ]


def arrival() -> dict[str, object]:
    """The contract's ``InboundArrival``, each member a reference names
    resolved, so the Worker reads every member's bound in one place."""

    schemas = contract()["components"]["schemas"]
    shape = schemas["InboundArrival"]
    members = {
        name: schemas[member["$ref"].rpartition("/")[2]] if "$ref" in member else member
        for name, member in shape["properties"].items()
    }
    return {**shape, "properties": members}


def _drawn(template: str, context: dict[str, object]) -> str:
    """``template`` in HQ's page frame, for nobody signed in: drawn for no
    request, so it holds no nonce, token or reader of its own."""

    drawn = render_to_string(template, {"SITE_NAME": settings.SEVERINO_SITE_NAME, **context})
    # Without the blank lines and trailing spaces template tags leave.
    return "".join(line.rstrip() + "\n" for line in drawn.splitlines() if line.strip())


def pages(served: list[dict[str, object]], **frame: object) -> dict[str, str]:
    """The Worker's static pages, by file: its root, then what any path the
    Worker does not answer is given. ``frame`` is what a page drawn for a
    test also carries (the gate's nonce), which the Worker's own have none of."""

    head = Page(title="Inbound events", lede="Receives HQ's signed webhooks")
    listed = [{"label": provider_label(str(route["provider"])), "events": route["events"]} for route in served]
    links = (("Demo", source.DEMO), ("Repository", source.REPOSITORY))
    root = {"page": head, "routes": listed, "links": links}
    return {
        ROOT: _drawn("control_plane/inbound_worker.html", {**root, **frame}),
        MISSING: _drawn("404.html", {"back": "Back to inbound events", **frame}),
    }


def _worker_files() -> dict[Path, str]:
    """Every file the Worker is deployed from that HQ derives, with its content."""

    worker = Path(settings.BASE_DIR) / "worker"
    served = routes()
    secrets = [*dict.fromkeys(str(route["secret_binding"]) for route in served), SOCKET_TOKEN]
    # The Worker runs for its own paths alone; every other request is a page
    # of public/, or its 404.html, and never invokes it.
    assets = {
        "directory": f"./{PUBLIC}",
        "not_found_handling": "404-page",
        "run_worker_first": [*(str(route["path"]) for route in served), SOCKET_PATH],
    }
    wrangler = {**SETTINGS, "assets": assets, "secrets": {"required": secrets}}
    generated = {"providers": served, "socket": SOCKET_PATH}
    return {
        worker / "wrangler.jsonc": HEADER + json.dumps(wrangler, indent=2) + "\n",
        worker / "src/generated/routes.json": json.dumps(generated, separators=(",", ":")) + "\n",
        worker / "src/generated/arrival.json": json.dumps(arrival(), indent=2) + "\n",
        **{worker / PUBLIC / name: page for name, page in pages(served).items()},
    }


def _digest(files: dict[Path, str]) -> str:
    # Each file's path in the checkout, then its content, so a renamed file
    # moves the tag as a changed one does. A SHA-256 in hex, within the 100
    # bytes Cloudflare allows a version's tag.
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        digest.update(f"{path.relative_to(settings.BASE_DIR)}\0{content}\0".encode())
    return digest.hexdigest()


def _tag_file() -> Path:
    return Path(settings.BASE_DIR) / "worker" / "src/generated/deploy-tag"


def deploy_tag() -> str:
    """The tag ``worker:deploy`` gives the Worker's version, read from the
    file it reads: a digest of every file HQ derives for it, so a deployed
    version whose tag differs was built from other declarations. Read rather
    than derived again, since the pages it covers draw through settings a
    deployment moves (the static storage, the site's name)."""

    return _tag_file().read_text().strip()


def emitted() -> dict[Path, str]:
    """Every file, with its content."""

    files = _worker_files()
    told = Path(settings.BASE_DIR) / "worker/spec/generated/facts.json"
    return {**files, _tag_file(): _digest(files) + "\n", told: json.dumps(examples(), indent=2) + "\n"}


def examples() -> list[dict[str, object]]:
    """Each declared fact's example body, the route and event it is sent
    as, and what the Worker forwards of it, which the Worker's spec posts and
    holds it to; never deployed, so outside the deploy tag."""

    return [
        {
            "path": spec.path,
            "event_header": spec.event_header,
            "event": event,
            "body": dict(fact.example),
            "told": fact.read(fact.example),
        }
        for spec in INBOUND.values()
        for event, fact in spec.facts.items()
    ]
