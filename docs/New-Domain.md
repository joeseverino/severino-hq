# New Domain

A new host domain is its own Django app (such as `hq/domains/expenses/`) plus one
`DomainDescriptor` in `HOST_DOMAINS` (`hq/platform/application/domains.py`). No other
file names it. The descriptor carries:

- `apps` and `mounts`: `hq/config/settings.py` installs the apps and `hq/config/urls.py`
  mounts each urlconf at its prefix.
- `navigation`: the bar.
- `integration`: providers for its resources (`<app>/specs.py`), other commands,
  attention items, dashboard cards, connections and calendars.
- `records`, for a plain record domain: one model created, updated and deleted
  by command ([Records](#records)).

`expenses` is the worked example; `hq/platform/application/tests/fixture_domain` is the
smallest one.

### Records

A `records` declaration yields:

- the commands `<noun>.create` (unless `create=False`), `<noun>.upsert` (with
  `upsert`), `<noun>.update` and `<noun>.delete`;
- the `write_<resource>` and `delete_<resource>` permissions, which operators
  hold, and MCP under its write and delete switches;
- deletion itself (`hq.platform.application.records.deleter`) and its count on
  the health reading.

Its web views take `RecordFormMixin` (create, update) and `RecordDeleteMixin`
from `hq/platform/application/writes.py` and name only their model. The form
is the command's fields, each drawn from the model's column of that name
(`records.record_form`), so a field added to the command and the model is on
the form with no form of its own to edit. A label is the column's
`verbose_name` and a help text the command field's `Describe`. What a column
cannot say (a file's control and the check of what it holds) goes on a
`ModelForm` the declaration names as `form`, which lists no fields.

The form sends its values to the domain's create or update capability
through `run_posted`, as every other door does, and the save runs the model's
`full_clean`. So put a field rule on the model (a validator or a constraint),
never on the form or the service.

## Rules

What a new host domain has to do before the gate passes, and what holds for
every change. Each rule is a test class, and the class docstring is the rule:

- `hq/platform/application/tests/test_domain_declaration.py` and
  `test_domains.py` there: the declaration alone, and no second roster of
  sections;
- `test_architecture.py`, `test_architecture_pages.py`,
  `test_architecture_primitives.py` and `test_architecture_styles.py` there:
  delivery, pages, the shared primitives and the stylesheet;
- `hq/platform/application/tests/test_view_writes.py` and
  `hq/platform/core/tests/test_speculation.py`: what a view may write, and
  that a GET writes nothing.

The lint gates hold the rest, each rule with its reason in its own file:
`lint:ast-grep` (`tools/lint/ast-grep/rules`), `lint:imports` (the contracts
in `pyproject.toml`'s `[tool.importlinter]`), `lint:js`, `lint:css`,
`lint:prose` and `lint:deptry`. An extension takes its own path:
[Extensions](Plugins.md).

Outside these, `mise run check` also needs the domain's migrations committed
(`makemigrations --check`), the API document regenerated (`manage.py
api_openapi`; `api_openapi --check`), and its tests in its `tests/` package.
