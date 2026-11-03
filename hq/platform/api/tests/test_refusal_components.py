"""Each refusal response is named in words for its status and the kind of call
that answers its codes: operations answering the same codes share it, two
different sets never share a name, and only a refusal that says "later"
declares a wait."""

from http import HTTPStatus
from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application.refusal_status import REFUSAL_STATUS

from .. import openapi, refusal_names, views
from ..fragments import pascal as _pascal


def _by_status(codes):
    answered: dict[int, set[str]] = {}
    for code in codes:
        answered.setdefault(REFUSAL_STATUS[code], set()).add(code)
    return {status: frozenset(held) for status, held in answered.items()}


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
        """Every set an operation can answer, however many come to: every endpoint's at a
        status, one code where no endpoint answers, and each kind of call's."""

        endpoint = _by_status(views.ENDPOINT_CODES)
        sets = {(status, codes) for status, codes in endpoint.items()}
        sets |= {(status, frozenset({code})) for code, status in REFUSAL_STATUS.items() if status not in endpoint}
        sets |= {
            (status, codes) for kind in refusal_names.call_kinds() for status, codes in _by_status(kind.codes).items()
        }
        names: dict[str, tuple[int, frozenset[str]]] = {}
        for status, codes in sorted(sets, key=lambda answered: (answered[0], sorted(answered[1]))):
            name = refusal_names.refusal_name(status, sorted(codes))
            with self.subTest(name=name):
                self.assertNotIn(name, names, (status, sorted(codes)))
                self.assertLessEqual(len(name), 40)
            names[name] = (status, codes)

    def test_a_set_no_kind_of_call_answers_fails_the_build_naming_it(self):
        # Two codes at a status no endpoint answers, that no one kind of call answers together.
        with self.assertRaisesRegex(openapi.OpenAPIError, "target_not_allowed.*target_required"):
            refusal_names.refusal_name(400, ["target_not_allowed", "target_required"])

    def test_a_refusal_of_several_codes_is_named_for_its_kind_and_status_never_its_codes(self):
        for status, _, schema_name, schema in self._answered():
            codes = _code_list(schema)
            phrase = _pascal(HTTPStatus(status).phrase)
            name = schema_name.removesuffix("Refusal")
            if len(codes) > 1:
                with self.subTest(name=name):
                    self.assertTrue(name.endswith(phrase))
                    kind = name.removesuffix(phrase)
                    self.assertFalse([code for code in codes if _pascal(code) in kind], codes)

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
