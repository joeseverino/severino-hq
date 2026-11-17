"""The declaration of a plain record domain, which a host section names as its ``records``."""

from dataclasses import dataclass

from django.utils.text import capfirst


@dataclass(frozen=True, slots=True)
class Records:
    """A plain record domain: one model created, changed and deleted by command.

    Declared once, it yields the create, update and delete capabilities, the
    write and delete permissions, deletion itself, the count on the health
    reading and the record's form (``records.record_form``); anything else is
    a command through ``integration.capabilities``. References are
    ``module:attribute`` strings so it stays importable from settings, before
    any model is.
    """

    # Names the commands: "expense" gives expense.create, expense.delete.
    noun: str
    # The readable resource the commands act on. Also names the permissions:
    # "expenses" gives write_expenses and delete_expenses.
    resource: str
    model: str
    # A key of ``integration_specs.TARGET_KINDS``, and the model field it names.
    target: str
    lookup: str
    command: str
    # Creates when given no target, updates when given one.
    save: str
    upsert: str = ""
    create: bool = True
    # What a person calls one record, where the noun reads wrong.
    title: str = ""
    # Given the record about to go, returns what to run once the delete commits.
    on_delete: str = ""
    # The records any reader may count; every row when empty.
    visible: str = ""
    # (verb, summary, label) where the derived wording would say less than it
    # should; an empty one keeps the derived.
    wording: tuple[tuple[str, str, str], ...] = ()
    # (verb, variant, words): a mode a page offers its command in (``CapabilitySpec.variants``).
    variants: tuple[tuple[str, str, str], ...] = ()
    # A ModelForm its form is built on, for what a column alone cannot say (a
    # file's control and the check of what it holds); its fields are still
    # the command's, so it lists none.
    form: str = ""

    @property
    def called(self) -> str:
        """What a sentence calls one record as it opens: "Content item", "Expense"."""

        return capfirst(self.title or self.noun)

    @property
    def write(self) -> str:
        return f"write_{self.resource}"

    @property
    def delete(self) -> str:
        return f"delete_{self.resource}"
