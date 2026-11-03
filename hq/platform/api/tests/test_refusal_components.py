"""Each refusal response is named for its status and codes alone: operations
answering the same codes share it, two different sets never share a name, and
only a refusal that says "later" declares a wait."""

from itertools import chain, combinations
from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application.refusal_status import REFUSAL_STATUS

from .. import openapi

# refusal_status.LATER_CODES, written out so the test runs against code that has none.
LATER_CODES = frozenset({"rate_limited", "too_many_calls"})


def _code_list(schema):
    code = schema["properties"]["error"]["properties"]["code"]
    return [code["const"]] if "const" in code else code["enum"]


def _last(reference):
    return reference.rsplit("/", 1)[-1]


class RefusalComponentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()
        cls.components = cls.document["components"]

    def _answered(self):
        """Each refusal an operation answers: its status, its response, and its schema's name and schema."""

        for operations in self.document["paths"].values():
            for operation in operations.values():
                for status, answered in operation["responses"].items():
                    if status == "200":
                        continue
                    response = self.components["responses"].get(_last(answered.get("$ref", "")), answered)
                    name = _last(response["content"]["application/json"]["schema"]["$ref"])
                    yield int(status), response, name, self.components["schemas"][name]

    def test_operations_answering_the_same_codes_share_one_response(self):
        named: dict[tuple[int, frozenset[str]], set[str]] = {}
        for status, _, name, schema in self._answered():
            named.setdefault((status, frozenset(_code_list(schema))), set()).add(name)
        for (status, codes), names in named.items():
            with self.subTest(status=status, codes=sorted(codes)):
                self.assertEqual(len(names), 1)

    def test_no_two_code_sets_reach_one_name(self):
        """Every set of codes each status has, however many operations come to answer."""

        names: dict[str, tuple[int, tuple[str, ...]]] = {}
        for status in sorted(set(REFUSAL_STATUS.values())):
            codes = sorted(code for code, answered in REFUSAL_STATUS.items() if answered == status)
            for chosen in chain.from_iterable(combinations(codes, size) for size in range(1, len(codes) + 1)):
                name = openapi.refusal_name(status, list(chosen))
                with self.subTest(name=name):
                    self.assertNotIn(name, names, (status, chosen))
                names[name] = (status, chosen)

    def test_a_name_two_code_sets_reach_fails_the_build(self):
        with mock.patch.object(openapi, "refusal_name", return_value="Same"), self.assertRaises(openapi.OpenAPIError):
            openapi.document()

    def test_only_a_refusal_that_says_later_declares_a_wait(self):
        later = {REFUSAL_STATUS[code] for code in LATER_CODES}
        for status, response, _, schema in self._answered():
            error = schema["properties"]["error"]
            codes = set(_code_list(schema))
            with self.subTest(status=status, codes=sorted(codes)):
                waits = status in later
                self.assertEqual("retry_after" in error["properties"], waits)
                self.assertEqual("Retry-After" in response.get("headers", {}), waits)
                always = waits and codes <= LATER_CODES
                self.assertEqual("retry_after" in error["required"], always)
                if waits:
                    header = self.components["headers"][_last(response["headers"]["Retry-After"]["$ref"])]
                    self.assertEqual(header["required"], always)
