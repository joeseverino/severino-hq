"""Liveness and readiness probes, answered without a credential."""

import os
import pkgutil
from importlib.util import find_spec
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.http import JsonResponse

from hq.platform.application.plugins import plugin_health
from hq.platform.core.static import collected
from hq.platform.core.write_bounds import NOTHING, writes


def shipped_migrations() -> set[tuple[str, str]]:
    """Every migration this image carries, by app label and name.

    Named as Django's loader names them (``MigrationLoader.load_disk``) but
    read from the directory listing alone, so a probe imports no migration
    and builds no graph. Every name on disk is recorded once ``migrate`` has
    run, a squash and the migrations it replaces included."""

    shipped = set()
    for config in apps.get_app_configs():
        module, _explicit = MigrationLoader.migrations_module(config.label)
        spec = find_spec(module) if module else None
        # A namespace package, an empty directory, holds no migrations.
        if spec is None or spec.submodule_search_locations is None or spec.origin is None:
            continue
        shipped.update(
            (config.label, name)
            for _, name, is_pkg in pkgutil.iter_modules(spec.submodule_search_locations)
            if not is_pkg and name[0] not in "_~"
        )
    return shipped


@writes(NOTHING)
@login_not_required
def health_live(_request):
    """Minimal process liveness probe; never touches an external dependency."""

    return JsonResponse({"status": "ok"})


@writes(NOTHING)
@login_not_required
def health_ready(request):
    """Prove HQ can safely serve traffic without disclosing configuration."""

    checks = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            checks["database"] = cursor.fetchone() == (1,)
        applied = set(MigrationRecorder(connection).migration_qs.values_list("app", "name"))
        checks["migrations"] = shipped_migrations() <= applied
    except Exception:  # noqa: BLE001 - readiness must fail closed
        checks["database"] = False
        checks["migrations"] = False

    writable_paths = (
        settings.MEDIA_ROOT,
        settings.EXPORTS_ROOT,
        Path(settings.DATABASES["default"]["NAME"]).parent,
    )
    checks["storage"] = all(path.is_dir() and os.access(path, os.W_OK) for path in writable_paths)
    # The image carries its assets and a start collects none, so an image
    # built without them is not ready. Read once, when the storage loads.
    checks["assets"] = settings.STATIC_LIVE or collected()
    # Aggregated for anonymous callers, itemized for signed-in ones.
    #
    # This endpoint answers without a credential, because a container
    # healthcheck cannot sign in. A probe only needs to know whether HQ can
    # serve traffic at all; which extension is unhealthy is an operator's
    # question, and is answered to operators.
    plugins = plugin_health()
    if plugins:
        checks["plugins"] = all(plugins.values())
        if request.user.is_authenticated:
            checks.update({f"plugin:{key}": value for key, value in plugins.items()})
    ready = all(checks.values())
    return JsonResponse(
        {"status": "ok" if ready else "unavailable", "checks": checks},
        status=200 if ready else 503,
    )
