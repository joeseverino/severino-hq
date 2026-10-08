"""Liveness and readiness probes, answered without a credential."""

import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.http import JsonResponse

from hq.platform.application.plugins import plugin_health
from hq.platform.core.static import collected


@login_not_required
def health_live(request):
    """Minimal process liveness probe; never touches an external dependency."""

    return JsonResponse({"status": "ok"})


@login_not_required
def health_ready(request):
    """Prove HQ can safely serve traffic without disclosing configuration."""

    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
        executor = MigrationExecutor(connection)
        checks["migrations"] = not executor.migration_plan(
            executor.loader.graph.leaf_nodes()
        )
    except Exception:  # noqa: BLE001 - readiness must fail closed
        checks["database"] = False
        checks["migrations"] = False

    writable_paths = (
        settings.MEDIA_ROOT,
        settings.EXPORTS_ROOT,
        Path(settings.DATABASES["default"]["NAME"]).parent,
    )
    checks["storage"] = all(
        path.is_dir() and os.access(path, os.W_OK) for path in writable_paths
    )
    # The image carries its assets and a start collects none, so an image
    # built without them is not ready. Read once, when the storage loads.
    checks["assets"] = settings.STATIC_LIVE or collected()
    # Aggregated for anonymous callers, itemised for signed-in ones.
    #
    # This endpoint answers without a credential, because a container
    # healthcheck cannot sign in. A probe only needs to know whether HQ can
    # serve traffic at all; which extension is unhealthy is an operator's
    # question, and is answered to operators.
    plugins = plugin_health()
    if plugins:
        checks["plugins"] = all(plugins.values())
        if getattr(request.user, "is_authenticated", False):
            checks.update({f"plugin:{key}": value for key, value in plugins.items()})
    ready = all(checks.values())
    return JsonResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status=200 if ready else 503,
    )
