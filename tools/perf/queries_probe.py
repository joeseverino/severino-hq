"""The queries each page makes, counted in the measured server's own code and database.

Never imported: its text is run as `manage.py shell -c` where the server runs
(a checkout, a worktree of the base, the image), so the count is that code's.
It prints one line, MARKER and a JSON object of each page in PERF_PAGES to its
count, or to None where the page did not answer 200.
"""

import json
import os

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

MARKER = "perf-queries "


def count(client: Client, page: str) -> int | None:
    # Asked once first, so what a first request caches is not its cost, and a
    # redirect followed, as the server time follows it.
    client.get(page, follow=True)
    with CaptureQueriesContext(connection) as captured:
        response = client.get(page, follow=True)
        response.getvalue()
    return len(captured.captured_queries) if response.status_code == 200 else None


def main() -> None:
    client = Client(HTTP_HOST="127.0.0.1", raise_request_exception=False)
    client.force_login(get_user_model()._default_manager.get_by_natural_key(os.environ["PERF_USER"]))
    pages = json.loads(os.environ["PERF_PAGES"])
    print(MARKER + json.dumps({page: count(client, page) for page in pages}))


main()
