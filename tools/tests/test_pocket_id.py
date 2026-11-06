"""wire-pocket-id against a fake Pocket ID: nothing leaves the machine.

The fake answers in Pocket ID v2.19.2's shapes (backend/internal/api/dto.go,
dto/pagination_dto.go, apperror's ErrorDto), and keeps its rules: a write
replaces the API's whole list, and a client's grant holds only the API's own
permission ids.
"""

import io
import re
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from hq.platform.application.tokens import DEVICE_SCOPE
from hq.platform.core.tests.task import Usage
from hq.platform.mcp.security import token_scopes
from tests.mise_tasks import body, mise_tasks
from tools import pocket_id
from tools.credentials import Call, Refused
from tools.pocket_id import DESCRIPTION_LIMIT, KEY_VARIABLE, NAME_LIMIT, Permission, Run

KEY = "admin-api-key-0123456789"
BASE = "https://id.example.test"
RESOURCE = "https://hq.example.test/api"
API_ID = "6f1c0d52-0000-4000-8000-000000000001"
CLIENT = "claude-code"


class PocketId:
    """The endpoints the task reaches, every call recorded."""

    def __init__(self, *, permissions=(), grants=None, resource=RESOURCE) -> None:
        self.calls: list[Call] = []
        self.resource = resource
        self.permissions = [
            {"id": f"old-{p}", "key": p, "name": p, "allowedForCimdClients": False} for p in permissions
        ]
        self.grants = grants or []

    def api(self) -> dict:
        return {
            "id": API_ID,
            "name": "HQ",
            "resource": self.resource,
            "createdAt": "2026-01-01T00:00:00Z",
            "permissions": self.permissions,
            "allowCimdClients": False,
        }

    def __call__(self, call: Call):
        self.calls.append(call)
        if call.header != "X-API-Key" or call.token != KEY:
            return {"error": "You are not signed in", "code": "not_signed_in"}
        path = urlsplit(call.url).path
        if (call.method, path) == ("GET", "/api/apis"):
            search = parse_qs(urlsplit(call.url).query).get("search", [""])[0]
            found = [self.api()] if search in self.resource else []
            return {"data": found, "pagination": {"totalPages": 1, "totalItems": len(found), "currentPage": 1}}
        if (call.method, path) == ("PUT", f"/api/apis/{API_ID}/permissions"):
            kept = {p["key"]: p["id"] for p in self.permissions}
            self.permissions = [
                {"id": kept.get(p["key"], f"new-{p['key']}"), "allowedForCimdClients": False, **p}
                for p in call.json["permissions"]
            ]
            return self.api()
        if (call.method, path) == ("GET", f"/api/api-access/{CLIENT}/apis"):
            return self.grants
        if (call.method, path) == ("PUT", f"/api/apis/{API_ID}/clients/{CLIENT}"):
            ids = {p["id"] for p in self.permissions}
            return {
                "userDelegatedAccess": call.json["userDelegatedAccess"],
                "clientAccess": call.json["clientAccess"],
                "userDelegatedPermissionIds": [i for i in call.json["userDelegatedPermissionIds"] if i in ids],
                "clientPermissionIds": [i for i in call.json["clientPermissionIds"] if i in ids],
            }
        return {"error": "Not found", "code": "not_found"}

    def sent(self, method: str, path: str) -> list[Call]:
        return [call for call in self.calls if call.method == method and urlsplit(call.url).path == f"/api{path}"]


def declared_flags() -> set[str]:
    usage = body(mise_tasks()["wire-pocket-id"])
    return {name.replace("-", "_") for name in re.findall(r'#USAGE flag\s+"--([\w-]+)', usage)}


class Wired:
    def __init__(self, fake: PocketId, flags: dict[str, str], *, key: str = KEY) -> None:
        undeclared = set(flags) - declared_flags()
        assert not undeclared, f"wire-pocket-id declares no {sorted(undeclared)}"
        self.fake, out, err = fake, io.StringIO(), io.StringIO()
        self.listed = pocket_id.catalog()
        environ = {KEY_VARIABLE: key} if key else {}
        self.refused: Refused | None = None
        try:
            pocket_id.wire(Run(Usage(flags), environ, fake, out, err), self.listed)
        except Refused as refused:
            self.refused = refused
        self.out, self.err = out.getvalue(), err.getvalue() + (str(self.refused) if self.refused else "")


def written(**flags: str) -> dict[str, str]:
    return {"pocket_id": BASE, "resource": RESOURCE, **flags}


class PermissionListTests(SimpleTestCase):
    def test_the_write_is_the_catalogs_token_scopes_and_the_device_permission(self):
        run = Wired(PocketId(permissions=("read", "retired_scope")), written())

        (put,) = run.fake.sent("PUT", f"/apis/{API_ID}/permissions")
        self.assertIsNone(run.refused)
        self.assertEqual([p["key"] for p in put.json["permissions"]], [*token_scopes(), DEVICE_SCOPE])
        self.assertEqual(put.json["permissions"], [permission.body() for permission in run.listed])
        self.assertIn("retired_scope", run.err)

    def test_every_name_and_description_fits_pocket_ids_limits_whole(self):
        listed = pocket_id.catalog()

        self.assertEqual([p.key for p in listed if len(p.name) > NAME_LIMIT], [])
        self.assertEqual([p.key for p in listed if len(p.description) > DESCRIPTION_LIMIT], [])

    def test_a_long_description_ends_on_a_whole_title_and_counts_the_rest(self):
        titles = [f"Command number {n} with a long title" for n in range(20)]

        text = pocket_id.described(titles)

        self.assertLessEqual(len(text), DESCRIPTION_LIMIT)
        *shown, rest = text.split("; ")
        self.assertEqual(set(shown) - set(titles), set())
        self.assertEqual(rest, f"and {len(titles) - len(shown)} more")

    def test_a_name_past_the_limit_is_refused_naming_its_key(self):
        long = "example_" + "x" * NAME_LIMIT

        with self.assertRaises(Refused) as refused:
            pocket_id.listed(("read", long), {}, DEVICE_SCOPE)

        self.assertIn(long, str(refused.exception))


class GrantTests(SimpleTestCase):
    def test_the_client_is_given_every_listed_permission_but_the_devices(self):
        run = Wired(PocketId(), written(grant=CLIENT))

        (put,) = run.fake.sent("PUT", f"/apis/{API_ID}/clients/{CLIENT}")
        ids = {p["key"]: p["id"] for p in run.fake.permissions}
        self.assertIsNone(run.refused)
        self.assertTrue(put.json["userDelegatedAccess"])
        self.assertEqual(put.json["userDelegatedPermissionIds"], [ids[scope] for scope in token_scopes()])
        self.assertNotIn(ids[DEVICE_SCOPE], put.json["userDelegatedPermissionIds"])

    def test_what_the_client_already_holds_on_its_own_and_its_device_standing_are_kept(self):
        device = f"old-{DEVICE_SCOPE}"
        held = {"clientAccess": True, "clientPermissionIds": ["old-read"], "userDelegatedPermissionIds": [device]}
        grants = [{"api": {"id": API_ID}, "userDelegatedAccess": True, **held}]

        run = Wired(PocketId(permissions=("read", DEVICE_SCOPE), grants=grants), written(grant=CLIENT))

        (put,) = run.fake.sent("PUT", f"/apis/{API_ID}/clients/{CLIENT}")
        self.assertEqual((put.json["clientAccess"], put.json["clientPermissionIds"]), (True, ["old-read"]))
        self.assertIn(device, put.json["userDelegatedPermissionIds"])

    def test_a_client_id_pocket_id_could_not_hold_is_refused_before_any_call(self):
        run = Wired(PocketId(), written(grant="../users"))

        self.assertIsNotNone(run.refused)
        self.assertEqual(run.fake.calls, [])


class KeyTests(SimpleTestCase):
    def test_without_the_key_the_list_is_printed_and_nothing_is_sent(self):
        run = Wired(PocketId(), written(grant=CLIENT), key="")

        self.assertIsNone(run.refused)
        self.assertEqual(run.fake.calls, [])
        self.assertEqual([line.split("\t")[0] for line in run.out.splitlines()], [*token_scopes(), DEVICE_SCOPE])

    def test_the_key_travels_in_its_header_alone(self):
        run = Wired(PocketId(), written(grant=CLIENT))

        self.assertTrue(run.fake.calls)
        for call in run.fake.calls:
            self.assertEqual(call.header, "X-API-Key")
            self.assertNotIn(KEY, call.url)
            self.assertNotIn(KEY, repr(call))
        self.assertNotIn(KEY, run.out + run.err)

    def test_a_refusal_carries_pocket_ids_reason_and_not_the_key(self):
        run = Wired(PocketId(), written(), key="wrong-key-0123456789")

        self.assertIn("not signed in", run.err)
        self.assertNotIn("wrong-key-0123456789", run.err + run.out)
        self.assertEqual(run.fake.sent("PUT", f"/apis/{API_ID}/permissions"), [])

    def test_an_api_of_another_resource_is_never_written(self):
        run = Wired(PocketId(resource=f"{RESOURCE}/other"), written())

        self.assertIsNotNone(run.refused)
        self.assertEqual(run.fake.sent("PUT", f"/apis/{API_ID}/permissions"), [])

    def test_the_task_takes_the_key_from_the_environment_alone(self):
        task = body(mise_tasks()["wire-pocket-id"])

        self.assertNotIn(KEY_VARIABLE, " ".join(re.findall(r"#USAGE .*", task)))
        self.assertFalse({"key", "api_key", "token"} & declared_flags())


class PermissionBodyTests(SimpleTestCase):
    def test_a_permission_without_commands_is_written_without_a_description(self):
        self.assertEqual(Permission("read", "Read").body(), {"key": "read", "name": "Read"})
