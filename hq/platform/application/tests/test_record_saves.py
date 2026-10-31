"""Every record saved with its links is held as its caller last read it."""

from datetime import date

from django.test import TestCase

from hq.domains.content.models import ContentItem
from hq.domains.projects.models import Project

from ..assets import AssetCommand, save_asset
from ..capabilities import execute_capability
from ..content import ContentCommand, save_content
from ..documentation import DocumentationCommand, save_documentation
from ..expenses import ExpenseCommand, save_expense
from ..record_errors import RecordConflict, RecordNotFound
from ..security import OPERATOR_CAPABILITIES, Principal, cli_principal

STALE = "2000-01-01T00:00:00+00:00"


def _saves():
    """Each record's save, a command for it, and the keyword its target goes by."""

    return (
        (save_asset, AssetCommand(item_name="Example box", slug="example-box"), "current_slug", "slug"),
        (save_content, ContentCommand(title="Example post", slug="example-post"), "current_slug", "slug"),
        (
            save_documentation,
            DocumentationCommand(doc_id="example-doc", title="Example doc"),
            "current_doc_id",
            "doc_id",
        ),
        (save_expense, ExpenseCommand(date=date(2026, 7, 25), vendor="Example", item="Thing"), "current_id", "id"),
    )


class HeldAsReadTests(TestCase):
    def test_a_record_that_is_not_there_is_not_found(self):
        for save, command, keyword, _key in _saves():
            with self.subTest(save.__name__), self.assertRaises(RecordNotFound):
                save(command, principal=cli_principal(), **{keyword: 999 if keyword == "current_id" else "missing"})

    def test_a_record_changed_since_it_was_read_is_refused_and_kept(self):
        for save, command, keyword, key in _saves():
            with self.subTest(save.__name__):
                created = save(command, principal=cli_principal())
                (record,) = (value for name, value in created.items() if name not in {"ok", "created"})
                with self.assertRaises(RecordConflict):
                    save(command, principal=cli_principal(), expected_updated_at=STALE, **{keyword: record[key]})
                updated = save(
                    command,
                    principal=cli_principal(),
                    expected_updated_at=record["updated_at"],
                    **{keyword: record[key]},
                )
                self.assertFalse(updated["created"])

    def test_a_target_named_by_another_records_keyword_is_refused(self):
        with self.assertRaises(TypeError):
            save_asset(AssetCommand(item_name="Example box"), principal=cli_principal(), current_id=1)


class LinkTests(TestCase):
    def test_a_link_to_one_record_is_set_by_its_key_and_cleared_by_none(self):
        Project.objects.create(name="Example", slug="example")
        ContentItem.objects.create(title="Example post", slug="example-post")
        command = ExpenseCommand(
            date=date(2026, 7, 25),
            vendor="Example",
            item="Thing",
            related_project="example",
            related_content="example-post",
        )
        saved = save_expense(command, principal=cli_principal())["expense"]
        self.assertEqual((saved["related_project"], saved["related_content"]), ("example", "example-post"))

        cleared = save_expense(
            ExpenseCommand(date=date(2026, 7, 25), vendor="Example", item="Thing", related_project=""),
            principal=cli_principal(),
            current_id=saved["id"],
        )["expense"]
        self.assertEqual((cleared["related_project"], cleared["related_content"]), (None, None))

    def test_a_set_of_links_is_replaced_whole(self):
        Project.objects.create(name="One", slug="example-one")
        Project.objects.create(name="Two", slug="example-two")
        first = save_documentation(
            DocumentationCommand(doc_id="example-doc", title="Example", related_projects=("example-one",)),
            principal=cli_principal(),
        )["documentation"]
        again = save_documentation(
            DocumentationCommand(doc_id="example-doc", title="Example", related_projects=("example-two",)),
            principal=cli_principal(),
            current_doc_id=first["doc_id"],
        )["documentation"]
        self.assertEqual(again["relationships"]["projects"], ["example-two"])


def _refused(name, payload, *, target=None, expected_updated_at=None):
    result = execute_capability(
        name,
        payload,
        principal=Principal("operator", "cli", OPERATOR_CAPABILITIES),
        target=target,
        expected_updated_at=expected_updated_at,
    )
    assert not result["ok"], result
    return result["error"]


# Each saved record, in the order of _saves: its capability's prefix, a payload
# that saves one, a target that names nothing, and the word its refusals name
# it with.
RECORDS = {
    "asset": ({"item_name": "Example box"}, "missing-example", "asset"),
    "content": ({"title": "Example post", "slug": "example-post"}, "missing-example", "writeup"),
    "documentation": ({"doc_id": "example-doc", "title": "Example doc"}, "missing-example", "documentation"),
    "expense": ({"date": "2026-07-25", "vendor": "Example", "item": "Thing"}, 999_999, "expense"),
}
# Each link a saved record names: the field, a value that names nothing, and
# the word its refusal names the missing record with.
LINKS = {
    "asset": (("related_projects", ["missing-example"], "project"),),
    "content": (
        ("related_projects", ["missing-example"], "project"),
        ("related_assets", ["missing-example"], "asset"),
        ("related_expenses", [999_999], "expense"),
        ("related_documentation", ["missing-example"], "documentation"),
    ),
    "documentation": (
        ("related_projects", ["missing-example"], "project"),
        ("related_assets", ["missing-example"], "asset"),
        ("related_expenses", [999_999], "expense"),
    ),
    "expense": (
        ("related_project", "missing-example", "project"),
        ("related_asset", "missing-example", "asset"),
        ("related_content", "missing-example", "writeup"),
        ("related_documentation", "missing-example", "documentation"),
    ),
}


class RefusalWordingTests(TestCase):
    """A refused save, answered by its command, says which kind of record was missing or stale."""

    def test_a_missing_record_is_named_by_its_kind(self):
        for prefix, (payload, missing, word) in RECORDS.items():
            with self.subTest(prefix):
                error = _refused(f"{prefix}.update", payload, target=missing)
                self.assertEqual(error["code"], "not_found")
                self.assertIn(word, error["message"])

    def test_a_stale_record_is_named_by_its_kind(self):
        for (save, command, _keyword, key), (prefix, (payload, _missing, word)) in zip(
            _saves(), RECORDS.items(), strict=True
        ):
            with self.subTest(prefix):
                created = save(command, principal=cli_principal())
                (record,) = (value for name, value in created.items() if name not in {"ok", "created"})
                error = _refused(f"{prefix}.update", payload, target=record[key], expected_updated_at=STALE)
                self.assertEqual(error["code"], "conflict")
                self.assertIn(word, error["message"])

    def test_a_missing_link_is_named_by_the_kind_it_links(self):
        for prefix, links in LINKS.items():
            payload = RECORDS[prefix][0]
            for field, value, word in links:
                with self.subTest(f"{prefix}.{field}"):
                    error = _refused(f"{prefix}.create", {**payload, field: value})
                    self.assertEqual(error["code"], "not_found")
                    self.assertIn(word, error["message"])
