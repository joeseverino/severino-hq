"""The inbound-events Worker's configuration, derived from the providers'
``INBOUND`` declarations, so the Worker verifies and forwards exactly the
events HQ takes.

``routes.json`` is the one place the Worker and its tests learn each
provider's route, and ``wrangler.jsonc`` requires each route's secret.
"""

import json
from dataclasses import fields
from pathlib import Path

from django.conf import settings

from .provider_adapters import INBOUND

# The Worker secret the listener's socket presents as its bearer: the
# Worker's own, never a provider's, so no INBOUND declaration names it.
SOCKET_TOKEN = "SOCKET_TOKEN"  # noqa: S105 - the Worker secret's name, never its value

HEADER = """\
// The inbound-events Worker (src/index.ts). The account and the route's
// hostname come from the deploy's environment, so this public file names
// neither. Written by `mise run manage -- inbound_worker` from the providers'
// INBOUND declarations.
"""

# What the Worker is, whatever the providers declare.
SETTINGS = {
    "$schema": "node_modules/wrangler/config-schema.json",
    "name": "hq-inbound",
    "main": "src/index.ts",
    "compatibility_date": "2026-11-23",
    "workers_dev": False,
    "exports": {"Hub": {"type": "durable-object", "storage": "sqlite"}},
    "durable_objects": {"bindings": [{"name": "HUB", "class_name": "Hub"}]},
}

# What the Worker never reads: HQ's readings for each event, and what the
# provider's app must hold.
OFF_ROUTE = {"events", "requires"}


def routes() -> list[dict[str, object]]:
    """Each provider the Worker receives events from: its route, then the
    events it forwards. A source that reports over the bridge alone has none."""

    return [
        {
            "provider": provider,
            **{field.name: getattr(spec, field.name) for field in fields(spec) if field.name not in OFF_ROUTE},
            "events": list(spec.events),
        }
        for provider, spec in INBOUND.items()
        if spec.path
    ]


def emitted() -> dict[Path, str]:
    """Both files, with their content."""

    worker = Path(settings.BASE_DIR) / "worker"
    served = routes()
    secrets = [*dict.fromkeys(str(route["secret_binding"]) for route in served), SOCKET_TOKEN]
    wrangler = {**SETTINGS, "secrets": {"required": secrets}}
    return {
        worker / "wrangler.jsonc": HEADER + json.dumps(wrangler, indent=2) + "\n",
        worker / "src/generated/routes.json": json.dumps({"providers": served}, separators=(",", ":")) + "\n",
    }
