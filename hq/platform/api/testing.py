"""Hold every API response a test receives to the OpenAPI document.

``ContractClient`` fails the test that receives an /api/ response, or has a
capability accept a request body, the document does not describe; the same
run can record the document's examples (beside ``_record``).
"""

import atexit
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, override
from urllib.parse import urlsplit

from django.http import HttpResponse
from django.test import Client
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .openapi import EXAMPLES_PATH, document

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse
    from django.utils.functional import _StrPromise

URI = "urn:hq:api"
# Large bodies (the registry catalogs, the document itself) are their own
# example; recording them would copy the registry into the document.
EXAMPLE_LIMIT = 2048

_recorded: dict[str, dict[str, Any]] = {}


class Contract:
    """One derived document, and how to find and validate its operations."""

    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value
        self.registry = Registry().with_resource(URI, Resource.from_contents(value, default_specification=DRAFT202012))
        self.templates = [
            (re.compile("^" + re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(path)) + "$"), path)
            for path in value["paths"]
            if "{" in path
        ]

    def path(self, url: str) -> str | None:
        """The documented path a URL is served by: concrete first, then templated."""

        if url in self.value["paths"]:
            return url
        return next((path for pattern, path in self.templates if pattern.match(url)), None)

    def validate(self, pointer: list[str], value: object) -> None:
        escaped = "/".join(part.replace("~", "~0").replace("/", "~1") for part in pointer)
        Draft202012Validator({"$ref": f"{URI}#/{escaped}"}, registry=self.registry).validate(value)

    def response_pointer(self, path: str, method: str, status: int) -> list[str] | None:
        responses = self.value["paths"][path][method]["responses"]
        response = responses.get(str(status))
        if response is None:
            return None
        if "$ref" in response:
            name = response["$ref"].rsplit("/", 1)[1]
            return ["components", "responses", name, "content", "application/json", "schema"]
        return ["paths", path, method, "responses", str(status), "content", "application/json", "schema"]


class ContractClient(Client):
    @cached_property
    def contract(self) -> Contract:
        return Contract(document())

    @override
    def generic(
        self,
        method: str,
        path: str | _StrPromise,
        data: Any = "",
        content_type: str | None = "application/octet-stream",
        secure: bool = False,
        *,
        headers: Mapping[str, str] | None = None,
        query_params: Mapping[Any, Any] | None = None,
        **extra: Any,
    ) -> _MonkeyPatchedWSGIResponse:
        response = super().generic(
            method,
            path,
            data,
            content_type,
            secure,
            headers=headers,
            query_params=query_params,
            **extra,
        )
        url = urlsplit(str(path)).path
        if url.startswith("/api/"):
            self._conform(method.lower(), url, data, response)
        return response

    def _conform(self, method: str, url: str, data: Any, response: HttpResponse | _MonkeyPatchedWSGIResponse) -> None:
        contract = self.contract
        path = contract.path(url)
        if path is None:
            raise AssertionError(f"{url} is not in the OpenAPI document.")
        body = json.loads(response.content)
        operation = contract.value["paths"][path].get(method)
        if operation is None:
            # A method the route does not serve: the uniform refusal.
            if response.status_code != 405:
                raise AssertionError(f"{method.upper()} {path} is undocumented and was not refused.")
            contract.validate(["components", "schemas", "Failure"], body)
            return
        pointer = contract.response_pointer(path, method, response.status_code)
        if pointer is None:
            raise AssertionError(
                f"{method.upper()} {path} answered {response.status_code}, which the document does not list."
            )
        contract.validate(pointer, body)
        request = None
        if response.status_code < 300 and data and "requestBody" in operation:
            request = json.loads(data)
            contract.validate(
                ["paths", path, method, "requestBody", "content", "application/json", "schema"],
                request,
            )
        if os.environ.get("HQ_API_RECORD_EXAMPLES") == "1":
            _record(operation["operationId"], response.status_code, body, request)


def _small(value: object) -> bool:
    return len(json.dumps(value)) <= EXAMPLE_LIMIT


# With HQ_API_RECORD_EXAMPLES=1 (and --parallel 1, so one process holds them
# all) the first small response per operation and status, and the first
# accepted request body per operation, are written to openapi-examples.json
# (or the path HQ_API_RECORD_EXAMPLES_TO names) when the run ends; `mise run
# fix:api-examples` runs every module that uses this client so. A recording
# reads the same every run, so lint:drift-api-examples can hold the file to it:
# the clock stands still at RECORDED_AT from the moment this module is imported,
# before any test makes a row, and each UUID is written as the one its place
# among them derives, since a test's uuid4 reads randomness the clock does not
# hold. Integer ids already repeat, the tests running in one order in one
# process.
RECORDING = os.environ.get("HQ_API_RECORD_EXAMPLES") == "1"
RECORDED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _hold_the_clock() -> None:
    import time_machine  # noqa: PLC0415 - a tools-group dependency, which the image's suite never loads

    time_machine.travel(RECORDED_AT, tick=False).start()


if RECORDING:
    _hold_the_clock()


def _fixed_ids(text: str) -> str:
    """``text`` with each distinct UUID replaced by one derived from the order it first appears in."""

    seen: dict[str, str] = {}
    return _UUID.sub(
        lambda found: seen.setdefault(found[0], str(uuid.uuid5(uuid.NAMESPACE_URL, f"example/{len(seen)}"))), text
    )


def _record(operation_id: str, status: int, body: object, request: object) -> None:
    if _small(body):
        entry = _recorded.setdefault(operation_id, {})
        entry.setdefault("responses", {}).setdefault(str(status), body)
    if request is not None and _small(request):
        _recorded.setdefault(operation_id, {}).setdefault("request", request)


@atexit.register
def _write_examples() -> None:
    if not RECORDING or not _recorded:
        return
    path = os.environ.get("HQ_API_RECORD_EXAMPLES_TO") or EXAMPLES_PATH
    Path(path).write_text(_fixed_ids(json.dumps(_recorded, indent=2, sort_keys=True)) + "\n", encoding="utf-8")
