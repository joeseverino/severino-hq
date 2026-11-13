"""An exemption from lint:openapi-breaking says why clients can take the break it admits."""

from django.test import SimpleTestCase

# Each form oasdiff matches an --err-ignore line to: an operation's change, a
# shared schema's and a security scheme's, worded as oasdiff 1.37.0's
# checker/localizations_src/en/messages.yaml words them.
ENTRIES = (
    "- in API GET /api/v2/findings/ the `query` request parameter `limit` became required",
    "- in components removed the schema `ExampleRow`",
    "- the security scheme `exampleAuth` was removed from the API",
)


def unexplained(lines: list[str]) -> list[int]:
    # Imported where used, so a ref without the gate reports the failure, not an import error.
    from tools.openapi_breaking import unexplained as checked

    return checked(lines)


class ExemptionReasonTests(SimpleTestCase):
    def test_an_entry_with_its_reason_is_admitted(self):
        for entry in ENTRIES:
            with self.subTest(entry=entry):
                self.assertEqual(unexplained([f"{entry}. Reason: no client sends it yet"]), [])

    def test_an_entry_without_a_reason_is_refused(self):
        for entry in ENTRIES:
            for line in (entry, f"{entry}. Reason:", f"{entry}. Reason:   "):
                with self.subTest(line=line):
                    self.assertEqual(unexplained(["# Admitted breaks", "", line]), [3])

    def test_a_match_oasdiff_makes_in_any_case_needs_a_reason(self):
        # oasdiff lowercases the line before it matches.
        self.assertEqual(unexplained(["- IN COMPONENTS REMOVED THE SCHEMA `ExampleRow`"]), [1])

    def test_prose_oasdiff_cannot_match_needs_no_reason(self):
        self.assertEqual(unexplained(["# Client breaks", "", "An entry is a list item."]), [])
