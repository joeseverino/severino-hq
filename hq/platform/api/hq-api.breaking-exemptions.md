# Client breaks the machine API admits

lint:openapi-breaking fails a change to hq-api.openapi.json that breaks a
client, unless a line here names it. An entry is a list item: the line the
gate printed for the change, then `Reason:` and why clients can take the
break; oasdiff reads entries as its `--err-ignore` list, which admits one
change of one operation. A severity line, `<check-id> info` then `Reason:`
and why, goes to oasdiff's `--severity-levels` and lowers every change that
check finds to info. A line oasdiff could read that gives no reason fails the
gate. Only lines added since the comparison base admit a break; lines already
on that base admit nothing.

api-security-scope-added info Reason: each operation's security now names the capabilities HQ already refused a call without, in place of an extension field; a token admitted before is admitted now

request-parameter-max-set info Reason: every bound it sets declares a limit the server already held: an id past the 64-bit column's ceiling was answered as a server error, and a findings limit is a page, at most 100 as every list in the API pages, while the summary still counts every finding

request-parameter-min-set info Reason: an id is a positive integer, and one below 1 named no record, so a client sent nothing that read one

request-property-all-of-added info Reason: each subschema it adds holds a command field to the rules its record's model column already refused a value outside in its full_clean, after the call was authorized and perhaps held for approval (a column's length, its blank, its slug pattern, its choices, a declaration's whole kind and spec); the command now refuses the value as it is validated, so no value the server stored is refused

request-property-became-enum info Reason: a pin's kind and a reading's kind were already refused by the capability's handler outside the kinds HQ has; the command's type now refuses them as it is validated and the schema lists them

request-property-max-length-set info Reason: every bound it sets is a model column's max_length, which the record's full_clean already refused past, or a hostname's 253 characters, past which a name lookup was already refused; the command now refuses it as it is validated

request-property-max-set info Reason: every bound it sets declares a limit the server already refused past: an id past the 64-bit column's ceiling was answered as a server error, a weekday past 6 and a business-use share past 100 were refused by the model

request-property-min-set info Reason: every bound it sets declares a limit the server already refused below: an id below 1 named no record, a weekday below 0 and a business-use share below 0 were refused by the model

request-property-one-of-removed info Reason: the six are the target of contact.submission.delete, contact.submission.review, expense.delete, expense.update, receipt.delete and receipt.update, whose two branches, an integer and its digits as text, gained the id's bounds (1 to the 64-bit column's ceiling, no sign); what they now refuse named no record or was answered as a server error, so a client sends nothing that worked before

request-property-pattern-added info Reason: every pattern it adds was already refused outside after the payload was taken: a slug column's pattern by the model's full_clean, a hostname, a blank pin key and a row's revision by the capability's handler, a URL column's address by its URLValidator, a run's host by the command's own check, a weather point by its parser, and a decimal's digits by pydantic's max_digits and decimal_places; the command now refuses it as it is validated and the schema says the pattern

request-property-any-of-removed info Reason: receipt.update's amount keeps its two branches, a number and its digits as text, which oasdiff reads as replaced once they gained the bounds, step and pattern of the column's max_digits and decimal_places, past which pydantic already refused the amount

request-parameter-became-enum info Reason: a list's choice filter was already a Literal in its query's type, refused outside its choices, and only its null branch left the document; a capability catalog's resource and a resource catalog's name are the registry's names, and one outside it named no resource

request-parameter-list-of-types-narrowed info Reason: a query parameter not sent is absent, so the null its query type allows was never a value a client could send in a query string; the document now says the parameter without it

request-parameter-max-length-set info Reason: every bound it sets is the query type's own max_length, which the server already refused past; only the parameter's null branch held it before, where oasdiff did not read it

request-parameter-min-length-set info Reason: a run's correlation and machine were already required to be non-empty by the query type, which the parameter now says without its null branch

request-parameter-pattern-added info Reason: a capability catalog's name is a capability's dotted name or a prefix of whole segments, so a name outside the pattern matched no capability, the infrastructure list's kinds are each a kind its kind filter lists, which the query's validator already refused outside, and each other pattern is the query type's own, which the server already refused outside

request-property-removed info Reason: a manifest entry is now one of three shapes, a vault doc, a draft and site content, each taking every field the entry took; the one key gone, system_service, the importer read only where system was absent, and the frontmatter the manifest copies names system

request-parameter-enum-value-removed info Reason: the generic read of one record lists only the resources found by text; one found by an integer refused a text identifier, and its own path, typed as it declares, still reads it at the same address

request-property-became-required info Reason: a TLS certificate's domains and install_on were refused empty by its spec's validator, so a declaration without them was never stored

request-property-min-items-set info Reason: a TLS certificate's domains and install_on were refused empty by its spec's validator; the schema now says the bound it held

request-property-pattern-changed info Reason: a URL's bracketed host is now an IPv6 address in the forms Django's URLValidator takes, and its user and password hold no bracket; the validator refused any other bracketed text after the call was validated

request-property-property-names-added info Reason: a container's by_design keys were refused outside the container standard's checks by its spec's validator; the schema now lists them

response-property-any-of-added info Reason: a list's items are its row or, under a mask of `*` or a record's path, its record, which the server already answered; every item is still an object, and the row's type is one of the two

response-property-type-changed info Reason: the same change: a list's items are typed as their row or their record, each an object, in place of the row alone

- error at hq/platform/api/hq-api.openapi.json, in API GET /api/v2/resources/action_items/ removed the enum value `hq.docs` from the property `anyOf[subschema #1]` of the `query` request parameter `source` [request-parameter-property-enum-value-removed]. Reason: docs raised one item, their review date, which HQ no longer tracks, so the source holds nothing to list; a client that filtered on hq.docs is refused where it had read an empty list
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/dashboard.panels.refresh/ removed the required property `data/requested_at` from the response with the `200` status [response-required-property-removed]. Reason: the time the panels were asked is the answer's standing (`data/standing`), which every ask now answers in one shape; no client acted on the timestamp
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/dashboard.panels.refresh_due/ removed the required property `data/requested_at` from the response with the `200` status [response-required-property-removed]. Reason: the time the panels were asked is the answer's standing (`data/standing`), which every ask now answers in one shape; no client acted on the timestamp
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/github.profile.read/ removed the required property `data/asked_at` from the response with the `200` status [response-required-property-removed]. Reason: when the read was asked is the answer's standing (`data/standing`) with its `status_url`, which every ask now answers in one shape; no client acted on the timestamp
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/infrastructure.controller.refresh/ removed the required property `data/requested` from the response with the `200` status [response-required-property-removed]. Reason: whether the read was asked is the answer's standing (`data/standing`), which every ask now answers in one shape
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/project.dependency_updates.run/ removed the required property `data/asked_at` from the response with the `200` status [response-required-property-removed]. Reason: when the read was asked is the answer's standing (`data/standing`) with its `status_url`, which every ask now answers in one shape; no client acted on the timestamp
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/project.dependency_updates.run/ removed the required property `data/requested` from the response with the `200` status [response-required-property-removed]. Reason: whether the read was asked is the answer's standing (`data/standing`), which every ask now answers in one shape
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/label` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/live` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/note` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/percent` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/seconds` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/state` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/status` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/visit.refresh/ removed the required property `data/tone` from the response with the `200` status [response-required-property-removed]. Reason: the reading's standing moved under `data/standing` and its follow link to `data/status_url`, the one shape every ask now answers; each field keeps its name and type there
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/certificate.renew/ removed the required property `data/policy` from the response with the `200` status [response-required-property-removed]. Reason: a renewal answers as every queued operation does, in the one shape asks answer; `policy.allowed` was always true on success, and its explanation is a policy sentence no answer carries
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/infrastructure.resource.remove/ added `#/components/schemas/OperationAsked` to the `data` response property `anyOf` list for the response status `200` [response-property-any-of-added]. Reason: the queued branch is renamed from OperationQueued to OperationAsked, every field it had kept with its type, beside the standing, status_url and message every ask answers

- error at hq/platform/api/hq-api.openapi.json, in API GET /api/v2/resources/action_items/ removed the enum value `hq.containers` from the property `anyOf[subschema #1]` of the `query` request parameter `source` [request-parameter-property-enum-value-removed]. Reason: this cleanup lists container image and hardening findings under hq.infrastructure; clients filtering on hq.containers must use hq.infrastructure instead. This branch explicitly admits that filter migration.

- error at hq/platform/api/hq-api.openapi.json, in API GET /api/v2/resources/action_items/ removed the enum value `hq.services` from the property `anyOf[subschema #1]` of the `query` request parameter `source` [request-parameter-property-enum-value-removed]. Reason: this cleanup lists service wiring findings under hq.infrastructure; clients filtering on hq.services must use hq.infrastructure instead. This branch explicitly admits that filter migration.
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/action_item.set_aside/ added 'propertyNames' constraint to the request property `payload/rows` [request-property-property-names-added]. Reason: a row's key was already refused by the handler when empty or longer than an item's key; the command's type now refuses it as it is validated
- error at hq/platform/api/hq-api.openapi.json, in API POST /api/v2/capabilities/lookup.address.read/ the `payload/address` request property `format` changed from `none` to `ipvanyaddress` [request-property-type-changed]. Reason: a value that is not an IP address was already refused by the handler; the command's type now refuses it as it is validated and the schema names the format
