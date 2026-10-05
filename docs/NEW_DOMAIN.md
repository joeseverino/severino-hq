# Adding a host domain

A new host domain is its own Django app (such as `hq/domains/expenses/`) plus one
`DomainDescriptor` in `HOST_DOMAINS` (`hq/platform/application/domains.py`). No other
file names it. The descriptor carries:

- `apps` and `mounts`: `hq/config/settings.py` installs the apps and `hq/config/urls.py`
  mounts each urlconf at its prefix.
- `navigation`: the bar.
- `integration`: providers for its resources (`<app>/specs.py`), other commands,
  attention items, dashboard cards, connections and calendars.
- `records`, for a plain record domain: one model created, updated and deleted
  by command. It yields `<noun>.create` (unless `create=False`), `<noun>.upsert`
  (with `upsert`), `<noun>.update` and `<noun>.delete`, the `write_<resource>`
  and `delete_<resource>` permissions (operators hold them; MCP under its write
  and delete switches), deletion itself (`hq.platform.application.records.deleter`) and
  its count on the health reading. Its web views take `RecordFormMixin`
  (create, update) and `RecordDeleteMixin` from `hq/platform/application/writes.py` and
  name only their `ModelForm`: the form saves through
  `hq.platform.application.records.save_form`, which runs the model's `full_clean` exactly
  as the command does, so put a field rule on the model (a validator or a
  constraint), never on the form or the service.

`expenses` is the worked example; `hq/platform/application/tests/fixture_domain` is the
smallest one.

What a new host domain has to do before the gate passes. Every row names the test that enforces it, in `Class.method` form.
`hq/platform/application/tests/test_new_domain.py` fails if an architecture rule exists that
this page does not name, or names one that is gone, so edit this page in the
same commit as the rule. Extensions are a different path: `docs/PLUGINS.md`.

## Steps

| Do | Enforced by |
|---|---|
| Keep views in `<app>/views.py` or `<app>/*_views.py`; a view in any other module escapes the checks below | `DeliveryAdapterArchitectureTests.test_every_module_defining_a_view_is_one_these_checks_read` |
| Views never call `.save()`, `.delete()` or `objects.create/update_or_create/bulk_*`; writes go through an `hq/platform/application/` service | `DeliveryAdapterArchitectureTests.test_web_views_do_not_mutate_models_directly` |
| A view with `paginate_by` takes `TableListMixin` | `DeliveryAdapterArchitectureTests.test_paginated_list_views_use_the_shared_table_engine` |
| `hq/platform/mcp/services.py` stays free of models and `.objects`; expose reads through `hq/platform/application/resources.py` and read models | `DeliveryAdapterArchitectureTests.test_mcp_services_do_not_access_django_models` |
| Keep the workflow layering: `workflow_contracts.py` has no relative imports, `ui.py` does not import `workflows`, `workflows.py` does not import `action_links` | `DeliveryAdapterArchitectureTests.test_workflow_models_remain_a_dependency_leaf` |
| Serve static assets from the shared delivery path: the ASGI mount is the only server of `/static/`, before Django; versioned files are immutable and sent as the gzip copy the image build wrote | `DeliveryAdapterArchitectureTests.test_asgi_routes_static_assets_before_django`, `DeliveryAdapterArchitectureTests.test_static_assets_have_one_server`, `DeliveryAdapterArchitectureTests.test_versioned_static_assets_are_compressed_and_immutable` |
| Resolve URLs with `hq.platform.application.routes.reverse`, never `django.urls.reverse`, in `hq/platform/application/` and `hq/domains/control_plane/` | `RouteOwnerTests.test_the_application_resolves_routes_through_its_one_owner`, `RouteOwnerTests.test_a_remembered_route_is_djangos_and_follows_the_url_configuration` |
| Every template sets `{% block title %}` to the page's own name, without the site name | `PageTitleTests.test_every_page_names_itself`, `PageTitleTests.test_no_page_appends_the_site_name_itself`, `PageTitleTests.test_the_layout_is_what_appends_it` |
| Link a command's form with `command_url`, never `reverse("command")` or `{% url "command" %}` | `OnePrimitiveTests.test_a_command_is_linked_through_command_url` |
| Name hosts with `normalized_hostname` and zone membership with `in_zone` | `OnePrimitiveTests.test_hostnames_are_spelled_by_normalized_hostname`, `OnePrimitiveTests.test_zone_membership_is_asked_of_in_zone` |
| Templates show ages with `ago`, byte counts with `bytes` (`human_bytes` in Python) and name entities with the entity tag; Python links entities with `entity_link` | `OnePrimitiveTests.test_templates_say_ages_through_the_ago_filter`, `OnePrimitiveTests.test_templates_say_byte_counts_through_the_bytes_filter`, `OnePrimitiveTests.test_python_says_byte_counts_through_human_bytes`, `OnePrimitiveTests.test_templates_name_entities_through_the_entity_tag`, `OnePrimitiveTests.test_entity_pages_are_addressed_by_entity_link` |
| Declare the domain once in `HOST_DOMAINS` (`hq/platform/application/domains.py`): a unique `hq.<name>` id, nav routes that resolve, grouped nav at order 100 or above (machinery at 900) | `DomainRegistryTests.test_domain_ids_are_unique`, `DomainRegistryTests.test_every_declared_route_resolves`, `DomainRegistryTests.test_host_sections_never_squat_the_extension_order_band`, `DomainRegistryTests.test_machinery_sorts_after_every_section_that_holds_work` |
| Keep no second roster of sections: no nav list in `hq/platform/core/context_processors.py`, no code-to-URL table in a core view | `DomainRegistryTests.test_the_registry_is_the_only_list_of_sections`, `DomainRegistryTests.test_the_view_keeps_no_code_to_url_table` |
| List its apps and mounts on the descriptor, never in `hq/config/settings.py` or `hq/config/urls.py` | `DomainRegistryTests.test_apps_and_urls_are_read_off_the_declarations` |
| Declare a record domain's create, update and delete and its permissions as `records`, never as entries in `core_capabilities.py` or members of `Capability` | `DomainRegistryTests.test_record_commands_and_permissions_are_derived_not_listed` |
| A part of a page that is fetched, refreshed or polled is a `data-fragment` region answered from a `{% partialdef %}` in the page's own template (`hq.platform.application.fragments`, `static/js/fragment.js`); a script never parses a response or keeps a timer of its own | `FragmentPrimitiveTests.test_only_the_primitive_parses_a_response_or_asks_again_on_a_timer`, `FragmentPrimitiveTests.test_the_primitive_is_one_fetch_one_parser_and_one_timer`, `FragmentPrimitiveTests.test_the_primitive_loads_before_the_scripts_that_use_it`, `FragmentPrimitiveTests.test_every_part_a_template_names_is_one_a_template_defines`, `FragmentPrimitiveTests.test_the_reads_it_replaced_stay_gone` |
| A GET has no effect. One that must record or reach out is refused to a speculative request by the audit writer and the outbound boundary (`hq.platform.core.speculation`) | `hq/platform/core/tests/test_speculation.py`, which prefetches every page and fails on one that writes |
| A view answers from what HQ holds and never waits on a network, a process or a timer: it asks the controller for a reading or starts a job, answers at once (`hq.platform.application.asks`), and draws the control with `partials/_ask.html`. A place a request must wait is a named entry in `hq.platform.core.outbound.ALLOWED` with its reason | `RequestNeverWaitsTests.test_every_request_is_served_under_the_rule`, `RequestNeverWaitsTests.test_an_exception_is_declared_with_its_reason_and_entered_only_where_listed`, `RequestNeverWaitsTests.test_only_a_job_leaves_the_request_that_started_it`, `RequestNeverWaitsTests.test_asked_for_work_is_followed_by_one_script_behaviour` |
| The declaration alone installs the app, mounts its URLs, puts it on the bar, registers its resource and commands, grants its permissions, deletes and counts it, and puts it in the API document | `NewDomainDeclarationTests.test_its_app_is_installed_and_its_urls_are_mounted`, `NewDomainDeclarationTests.test_it_is_on_the_bar`, `NewDomainDeclarationTests.test_its_resource_and_commands_are_registered`, `NewDomainDeclarationTests.test_its_permissions_are_granted_where_record_permissions_are`, `NewDomainDeclarationTests.test_it_is_created_counted_and_deleted_through_its_commands`, `NewDomainDeclarationTests.test_its_commands_and_resource_are_in_the_api_document` |

Outside the architecture tests, `mise run check` also needs its migrations
committed (`makemigrations --check`), the API document regenerated
(`manage.py api_openapi`; `api_openapi --check`), and its tests in a `tests/`
package or a `test*.py` module.

## Always on

Rules that hold for every change, not a domain in particular; the class docstring
is the rule. `StyleContractTests` and `SharedPrimitiveStyleTests` (CSS, tokens,
layers, spacing, colour), `TemplateCommentTests` (no multi-line `{# #}`),
`CognitiveComplexityTests` (score 20 per function), `CommentHistoryTests` (comments
describe the present), `AssertionPrecisionTests` (`assertEqual(a, b)`, not
`assertTrue(a == b)`), `SourceEscapeTests` (no invalid string escapes),
`CountedTests` (plurals), `InterfaceTextTests` (no em dash, hand-built plural,
nested form or `counted` phrase that cannot agree, in HQ or an extension; no
system check reads source), `PostButtonTests` (the shared post button),
`WorkflowSecrecyTests` (no secret interpolated into a workflow script),
`ComposedQueueTests` (a domain's attention items and cards reach the shared queue).
