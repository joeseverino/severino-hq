"""The queries each page makes, counted in the measured server's own code and database.

Never imported: its text is run as `manage.py shell -c` where the server runs
(a checkout, a worktree of the base, the image), so the count is that code's.
It prints one line, MARKER and a JSON object of each page in PERF_PAGES to its
counts, or to None where the page did not answer 200.

A page's queries are counted by the statement each sends, its SQL before its
parameters are bound. The first of each statement is the page's own cost;
every repeat of one is a query the page runs again for another of its rows
(N+1), so `repeated_queries` grows with the rows a page draws and the rest do
not.
"""

import json
import os
from collections import Counter

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client

MARKER = "perf-queries "


def count(client: Client, page: str) -> dict[str, int] | None:
    # Asked once first, so what a first request caches is not its cost, and a
    # redirect followed, as the server time follows it.
    client.get(page, follow=True)
    sent: Counter[str] = Counter()

    def record(execute, sql, params, many, context):
        sent[sql] += 1
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        response = client.get(page, follow=True)
        response.getvalue()
    if response.status_code != 200:
        return None
    total = sum(sent.values())
    return {"queries": total, "repeated_queries": total - len(sent)}


def main() -> None:
    client = Client(HTTP_HOST="127.0.0.1", raise_request_exception=False)
    client.force_login(get_user_model()._default_manager.get_by_natural_key(os.environ["PERF_USER"]))
    pages = json.loads(os.environ["PERF_PAGES"])
    print(MARKER + json.dumps({page: count(client, page) for page in pages}))


main()
