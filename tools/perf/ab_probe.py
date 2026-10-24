"""One side of `perf:ab`: a tree's own Django rendering pages through its test client.

Never imported: its text is run as `manage.py shell -c` in the tree measured
(this checkout, or a worktree of the ref), so every page is that code's. It migrates
the new database its environment names, signs up the operator, seeds the
example estate as dev:scratch does, and prints READY with the pages the tree's
navigation lists. Then it answers each line on stdin, a JSON object naming a
page, with one line: MARKER and a JSON object of the page's status, the time
its Server-Timing header says the server spent (and the time the client saw),
its body's size, and when asked its HTML and the queries it made.
"""

import contextlib
import io
import json
import os
import sys
import time

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

READY = "perf-ab-ready "
MARKER = "perf-ab "


def navigation() -> list[str]:
    """The pages the layout survey walks at this tree, or none where it has no such list."""

    try:
        from hq.platform.core.tests.browser.estate import navigation as listed  # noqa: PLC0415 - absent at an older ref
    except ImportError:
        return []
    return listed()


def answer(client: Client, ask: dict) -> dict:
    # Queries are captured only when asked, since capturing costs time of its own.
    captured = CaptureQueriesContext(connection) if ask.get("queries") else contextlib.nullcontext()
    with captured:
        started = time.perf_counter()
        response = client.get(ask["page"])
        body = response.getvalue()
        wall_ms = (time.perf_counter() - started) * 1000
    said = {
        "status": response.status_code,
        "timing": response.get("Server-Timing", ""),
        "wall_ms": round(wall_ms, 3),
        "bytes": len(body),
    }
    if ask.get("queries"):
        said["queries"] = len(captured.captured_queries)
    if ask.get("html"):
        said["html"] = body.decode("utf-8", errors="replace")
    return said


def main() -> None:
    call_command("migrate", interactive=False, verbosity=0)
    operator = get_user_model()._default_manager.create_superuser(
        os.environ["PERF_USER"], "operator@example.invalid", None
    )
    call_command("seed_demo", verbosity=0, stdout=io.StringIO())
    client = Client(HTTP_HOST="127.0.0.1", raise_request_exception=False)
    client.force_login(operator)
    print(READY + json.dumps(navigation()), flush=True)
    for line in sys.stdin:
        print(MARKER + json.dumps(answer(client, json.loads(line))), flush=True)


main()
