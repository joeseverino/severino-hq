"""The secret shapes strike nothing the contract says or a provider was recorded answering,
and a strike leaves a payload the contract still takes."""

import json
import string
import tempfile
from pathlib import Path
from typing import Any
from unittest import skipUnless

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.domains.control_plane.bridge_contract import contract
from hq.domains.control_plane.secret_shapes import struck

try:
    from hypothesis import given, settings as examples, strategies as st
    from hypothesis.configuration import set_hypothesis_home_dir
except ImportError:  # pragma: no cover - the image does not carry it
    HYPOTHESIS = False
else:
    HYPOTHESIS = True
    # Outside the checkout, as test_properties.py says.
    set_hypothesis_home_dir(str(Path(tempfile.gettempdir()) / "severino-hq-hypothesis"))

ROOT = Path(settings.BASE_DIR)
_REFERENCE = "#/components/schemas/"


def _strings(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)
    elif isinstance(value, str):
        yield value


def _request_schemas() -> list[tuple[str, dict[str, Any]]]:
    """Every schema node a request body reaches, by where it was found."""

    document = contract()
    components = document["components"]["schemas"]
    found: list[tuple[str, dict[str, Any]]] = []
    followed: set[str] = set()

    def walk(node: Any, where: str) -> None:
        if isinstance(node, dict):
            found.append((where, node))
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith(_REFERENCE):
                name = reference.removeprefix(_REFERENCE)
                if name not in followed:
                    followed.add(name)
                    walk(components[name], name)
            for key, child in node.items():
                walk(child, f"{where}/{key}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{where}/{index}")

    for path, item in document["paths"].items():
        body = item["post"].get("requestBody")
        if body:
            walk(body["content"]["application/json"]["schema"], path)
    return found


class ContractTests(SimpleTestCase):
    def test_no_word_of_the_contract_reads_as_a_secret(self):
        # Its names, enums, defaults, patterns and descriptions, each as a value.
        for text in set(_strings(contract())):
            with self.subTest(text=text[:80]):
                self.assertEqual(struck(text), (text, []))

    def test_no_string_a_request_carries_is_held_to_a_shape_a_strike_could_break(self):
        # A strike only shortens a string (every match is longer than what
        # replaces it), so only a pattern or a least length could refuse the
        # struck payload, and that would let an outsider's words stop a sync.
        for where, node in _request_schemas():
            with self.subTest(where=where):
                self.assertNotIn("pattern", node)
                self.assertNotIn("minLength", node)


@tag("source")
class RecordedPayloadTests(SimpleTestCase):
    def test_every_recorded_provider_answer_passes(self):
        recordings = sorted((ROOT / "controller" / "providers" / "testdata").glob("*.json"))
        self.assertTrue(recordings)
        for path in recordings:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertEqual(struck(json.loads(path.read_text()))[1], [])


def _shape(value: Any) -> Any:
    """``value`` with each string read as one placeholder: what a strike may change."""

    if isinstance(value, str):
        return str
    if isinstance(value, list):
        return [_shape(child) for child in value]
    if isinstance(value, dict):
        return {key: _shape(child) for key, child in value.items()}
    return value


if HYPOTHESIS:
    # Names a record carries, several of them read as a secret's.
    NAME = st.sampled_from(
        ("name", "high", "count", "token", "api_token", "Password", "client_secret", "private", "credentials")
    )
    VALUE = st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(string.ascii_lowercase, max_size=12),
        lambda children: st.lists(children, max_size=4) | st.dictionaries(NAME, children, max_size=4),
        max_leaves=24,
    )


@skipUnless(HYPOTHESIS, "hypothesis is a development tool")
class StrikeKeepsTheShapeTests(SimpleTestCase):
    if HYPOTHESIS:

        @examples(max_examples=300, deadline=None)
        @given(VALUE)
        def test_a_strike_replaces_only_a_string(self, value):
            # A reader of a record finds the counts, flags and structure it was
            # sent, whatever a member is named.
            self.assertEqual(_shape(struck(value)[0]), _shape(value))
