"""The development servers' application: the production one, on a schema brought up to the code first.

Uvicorn's reloader starts a new worker for each change, and the worker calls
this factory before it takes a connection, so a change that brings a
migration is applied before any page reads the new schema, as the container
migrates at boot. When nothing is new it costs a directory listing and one
query.
"""

from concurrent.futures import ThreadPoolExecutor

lazy from django.core.management import call_command
lazy from django.db import connection
lazy from django.db.migrations.recorder import MigrationRecorder

lazy from hq.config.asgi import application
lazy from hq.platform.core.health_views import shipped_migrations
lazy from hq.platform.core.outside_requests import connections_of_its_own


def migrate_new() -> None:
    """Apply what the checkout ships and the database has not recorded, by readiness's own measure.

    Run on a thread Django did not start, so its connection is its own.
    """

    with connections_of_its_own():
        applied = set(MigrationRecorder(connection).migration_qs.values_list("app", "name"))
        if not shipped_migrations() <= applied:
            call_command("migrate", interactive=False, verbosity=1)


def migrated_application():
    """The ASGI application, once ``migrate_new`` has run.

    Uvicorn calls a factory inside its running event loop, where Django
    refuses a query, so the migration runs on a thread of its own while the
    worker waits for it, and its failure stops the worker.
    """

    # Loaded first, since loading it sets Django up for the query.
    served = application
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="migrate") as pool:
        pool.submit(migrate_new).result()
    return served
